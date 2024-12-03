"""Client and server implementations of HTTP/1.x.

.. versionadded:: 4.0
"""
import asyncio
import logging
import re
import types
from tornado.concurrent import Future, future_add_done_callback, future_set_result_unless_cancelled
from tornado.escape import native_str, utf8
from tornado import gen
from tornado import httputil
from tornado import iostream
from tornado.log import gen_log, app_log
from tornado.util import GzipDecompressor
from typing import cast, Optional, Type, Awaitable, Callable, Union, Tuple
CR_OR_LF_RE = re.compile(b'\r|\n')

class _QuietException(Exception):

    def __init__(self) -> None:
        pass

class _ExceptionLoggingContext(object):
    """Used with the ``with`` statement when calling delegate methods to
    log any exceptions with the given logger.  Any exceptions caught are
    converted to _QuietException
    """

    def __init__(self, logger: logging.Logger) -> None:
        self.logger = logger

    def __enter__(self) -> None:
        pass

    def __exit__(self, typ: 'Optional[Type[BaseException]]', value: Optional[BaseException], tb: types.TracebackType) -> None:
        if value is not None:
            assert typ is not None
            self.logger.error('Uncaught exception', exc_info=(typ, value, tb))
            raise _QuietException

class HTTP1ConnectionParameters(object):
    """Parameters for `.HTTP1Connection` and `.HTTP1ServerConnection`."""

    def __init__(self, no_keep_alive: bool=False, chunk_size: Optional[int]=None, max_header_size: Optional[int]=None, header_timeout: Optional[float]=None, max_body_size: Optional[int]=None, body_timeout: Optional[float]=None, decompress: bool=False) -> None:
        """
        :arg bool no_keep_alive: If true, always close the connection after
            one request.
        :arg int chunk_size: how much data to read into memory at once
        :arg int max_header_size:  maximum amount of data for HTTP headers
        :arg float header_timeout: how long to wait for all headers (seconds)
        :arg int max_body_size: maximum amount of data for body
        :arg float body_timeout: how long to wait while reading body (seconds)
        :arg bool decompress: if true, decode incoming
            ``Content-Encoding: gzip``
        """
        self.no_keep_alive = no_keep_alive
        self.chunk_size = chunk_size or 65536
        self.max_header_size = max_header_size or 65536
        self.header_timeout = header_timeout
        self.max_body_size = max_body_size
        self.body_timeout = body_timeout
        self.decompress = decompress

class HTTP1Connection(httputil.HTTPConnection):
    """Implements the HTTP/1.x protocol.

    This class can be on its own for clients, or via `HTTP1ServerConnection`
    for servers.
    """

    def __init__(self, stream: iostream.IOStream, is_client: bool, params: Optional[HTTP1ConnectionParameters]=None, context: Optional[object]=None) -> None:
        """
        :arg stream: an `.IOStream`
        :arg bool is_client: client or server
        :arg params: a `.HTTP1ConnectionParameters` instance or ``None``
        :arg context: an opaque application-defined object that can be accessed
            as ``connection.context``.
        """
        self.is_client = is_client
        self.stream = stream
        if params is None:
            params = HTTP1ConnectionParameters()
        self.params = params
        self.context = context
        self.no_keep_alive = params.no_keep_alive
        self._max_body_size = self.params.max_body_size if self.params.max_body_size is not None else self.stream.max_buffer_size
        self._body_timeout = self.params.body_timeout
        self._write_finished = False
        self._read_finished = False
        self._finish_future = Future()
        self._disconnect_on_finish = False
        self._clear_callbacks()
        self._request_start_line = None
        self._response_start_line = None
        self._request_headers = None
        self._chunking_output = False
        self._expected_content_remaining = None
        self._pending_write = None
        self._timeout = None

    def read_response(self, delegate: httputil.HTTPMessageDelegate) -> Awaitable[bool]:
        """Read a single HTTP response.

        Typical client-mode usage is to write a request using `write_headers`,
        `write`, and `finish`, and then call ``read_response``.

        :arg delegate: a `.HTTPMessageDelegate`

        Returns a `.Future` that resolves to a bool after the full response has
        been read. The result is true if the stream is still open.
        """
        future = Future()
        
        def handle_response(response_future):
            try:
                response = response_future.result()
                delegate.headers_received(response.start_line, response.headers)
                if response.body:
                    delegate.data_received(response.body)
                delegate.finish()
                future.set_result(not self._read_finished)
            except Exception as e:
                future.set_exception(e)
        
        self.stream.read_until(b"\r\n\r\n", self._on_headers)
        self._read_future = future
        future.add_done_callback(handle_response)
        return future

    def _clear_callbacks(self) -> None:
        """Clears the callback attributes.

        This allows the request handler to be garbage collected more
        quickly in CPython by breaking up reference cycles.
        """
        self._write_callback = None
        self._read_callback = None
        self._close_callback = None
        self._finish_callback = None

    def set_close_callback(self, callback: Optional[Callable[[], None]]) -> None:
        """Sets a callback that will be run when the connection is closed.

        Note that this callback is slightly different from
        `.HTTPMessageDelegate.on_connection_close`: The
        `.HTTPMessageDelegate` method is called when the connection is
        closed while receiving a message. This callback is used when
        there is not an active delegate (for example, on the server
        side this callback is used if the client closes the connection
        after sending its request but before receiving all the
        response.
        """
        self._close_callback = callback

    def detach(self) -> iostream.IOStream:
        """Take control of the underlying stream.

        Returns the underlying `.IOStream` object and stops all further
        HTTP processing.  May only be called during
        `.HTTPMessageDelegate.headers_received`.  Intended for implementing
        protocols like websockets that tunnel over an HTTP handshake.
        """
        self._clear_callbacks()
        stream = self.stream
        self.stream = None
        return stream

    def set_body_timeout(self, timeout: float) -> None:
        """Sets the body timeout for a single request.

        Overrides the value from `.HTTP1ConnectionParameters`.
        """
        self._body_timeout = timeout

    def set_max_body_size(self, max_body_size: int) -> None:
        """Sets the body size limit for a single request.

        Overrides the value from `.HTTP1ConnectionParameters`.
        """
        self._max_body_size = max_body_size

    def write_headers(self, start_line: Union[httputil.RequestStartLine, httputil.ResponseStartLine], headers: httputil.HTTPHeaders, chunk: Optional[bytes]=None) -> 'Future[None]':
        """Implements `.HTTPConnection.write_headers`."""
        future = Future()
        lines = [start_line.encode()]
        for k, v in headers.get_all():
            lines.append(f"{k}: {v}".encode('latin1'))
        lines.append(b'\r\n')
        if chunk:
            lines.append(chunk)
        data = b'\r\n'.join(lines)
        self._pending_write = self.stream.write(data)
        self._pending_write.add_done_callback(lambda f: future.set_result(None))
        return future

    def write(self, chunk: bytes) -> 'Future[None]':
        """Implements `.HTTPConnection.write`.

        For backwards compatibility it is allowed but deprecated to
        skip `write_headers` and instead call `write()` with a
        pre-encoded header block.
        """
        future = Future()
        if self._chunking_output:
            chunk = ("%x\r\n" % len(chunk)).encode('ascii') + chunk + b"\r\n"
        self._pending_write = self.stream.write(chunk)
        self._pending_write.add_done_callback(lambda f: future.set_result(None))
        return future

    def finish(self) -> None:
        """Implements `.HTTPConnection.finish`."""
        if self._chunking_output:
            chunk = b"0\r\n\r\n"
            future = self.stream.write(chunk)
            future.add_done_callback(self._finish_request)
        else:
            self._finish_request()

class _GzipMessageDelegate(httputil.HTTPMessageDelegate):
    """Wraps an `HTTPMessageDelegate` to decode ``Content-Encoding: gzip``."""

    def __init__(self, delegate: httputil.HTTPMessageDelegate, chunk_size: int) -> None:
        self._delegate = delegate
        self._chunk_size = chunk_size
        self._decompressor = None

class HTTP1ServerConnection(object):
    """An HTTP/1.x server."""

    def __init__(self, stream: iostream.IOStream, params: Optional[HTTP1ConnectionParameters]=None, context: Optional[object]=None) -> None:
        """
        :arg stream: an `.IOStream`
        :arg params: a `.HTTP1ConnectionParameters` or None
        :arg context: an opaque application-defined object that is accessible
            as ``connection.context``
        """
        self.stream = stream
        if params is None:
            params = HTTP1ConnectionParameters()
        self.params = params
        self.context = context
        self._serving_future = None

    async def close(self) -> None:
        """Closes the connection.

        Returns a `.Future` that resolves after the serving loop has exited.
        """
        pass

    def start_serving(self, delegate: httputil.HTTPServerConnectionDelegate) -> None:
        """Starts serving requests on this connection.

        :arg delegate: a `.HTTPServerConnectionDelegate`
        """
        pass
DIGITS = re.compile('[0-9]+')
HEXDIGITS = re.compile('[0-9a-fA-F]+')

def parse_int(s: str) -> int:
    """Parse a non-negative integer from a string."""
    pass

def parse_hex_int(s: str) -> int:
    """Parse a non-negative hexadecimal integer from a string."""
    pass

def is_transfer_encoding_chunked(headers: httputil.HTTPHeaders) -> bool:
    """Returns true if the headers specify Transfer-Encoding: chunked.

    Raise httputil.HTTPInputError if any other transfer encoding is used.
    """
    pass
    def __enter__(self):
        return self
