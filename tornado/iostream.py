"""Utility classes to write to and read from non-blocking files and sockets.

Contents:

* `BaseIOStream`: Generic interface for reading and writing.
* `IOStream`: Implementation of BaseIOStream using non-blocking sockets.
* `SSLIOStream`: SSL-aware version of IOStream.
* `PipeIOStream`: Pipe-based IOStream implementation.
"""
import asyncio
import collections
import errno
import io
import numbers
import os
import socket
import ssl
import sys
import re
from tornado.concurrent import Future, future_set_result_unless_cancelled
from tornado import ioloop
from tornado.log import gen_log
from tornado.netutil import ssl_wrap_socket, _client_ssl_defaults, _server_ssl_defaults
from tornado.util import errno_from_exception
import typing
from typing import Union, Optional, Awaitable, Callable, Pattern, Any, Dict, TypeVar, Tuple
from types import TracebackType
if typing.TYPE_CHECKING:
    from typing import Deque, List, Type
_IOStreamType = TypeVar('_IOStreamType', bound='IOStream')
_ERRNO_CONNRESET = (errno.ECONNRESET, errno.ECONNABORTED, errno.EPIPE, errno.ETIMEDOUT)
if hasattr(errno, 'WSAECONNRESET'):
    _ERRNO_CONNRESET += (errno.WSAECONNRESET, errno.WSAECONNABORTED, errno.WSAETIMEDOUT)
if sys.platform == 'darwin':
    _ERRNO_CONNRESET += (errno.EPROTOTYPE,)
_WINDOWS = sys.platform.startswith('win')

class StreamClosedError(IOError):
    """Exception raised by `IOStream` methods when the stream is closed.

    Note that the close callback is scheduled to run *after* other
    callbacks on the stream (to allow for buffered data to be processed),
    so you may see this error before you see the close callback.

    The ``real_error`` attribute contains the underlying error that caused
    the stream to close (if any).

    .. versionchanged:: 4.3
       Added the ``real_error`` attribute.
    """

    def __init__(self, real_error: Optional[BaseException]=None) -> None:
        super().__init__('Stream is closed')
        self.real_error = real_error

class UnsatisfiableReadError(Exception):
    """Exception raised when a read cannot be satisfied.

    Raised by ``read_until`` and ``read_until_regex`` with a ``max_bytes``
    argument.
    """
    pass

class StreamBufferFullError(Exception):
    """Exception raised by `IOStream` methods when the buffer is full."""

class _StreamBuffer(object):
    """
    A specialized buffer that tries to avoid copies when large pieces
    of data are encountered.
    """

    def __init__(self) -> None:
        self._buffers = collections.deque()
        self._first_pos = 0
        self._size = 0
        self._large_buf_threshold = 2048

    def __len__(self) -> int:
        return self._size
    _large_buf_threshold = 2048

    def append(self, data: Union[bytes, bytearray, memoryview]) -> None:
        """
        Append the given piece of data (should be a buffer-compatible object).
        """
        if len(data) > self._large_buf_threshold:
            self._buffers.append(memoryview(data))
        else:
            self._buffers.append(bytes(data))
        self._size += len(data)

    def peek(self, size: int) -> memoryview:
        """
        Get a view over at most ``size`` bytes (possibly fewer) at the
        current buffer position.
        """
        if not self._buffers:
            return memoryview(b"")
        
        first_buffer = self._buffers[0]
        if len(first_buffer) - self._first_pos >= size:
            return memoryview(first_buffer)[self._first_pos:self._first_pos + size]
        
        view = bytearray(size)
        pos = 0
        for buf in self._buffers:
            if pos == size:
                break
            if pos == 0:
                chunk = buf[self._first_pos:]
            else:
                chunk = buf
            if len(chunk) > size - pos:
                chunk = chunk[:size - pos]
            view[pos:pos + len(chunk)] = chunk
            pos += len(chunk)
        
        return memoryview(view[:pos])

    def advance(self, size: int) -> None:
        """
        Advance the current buffer position by ``size`` bytes.
        """
        self._size -= size
        while size > 0 and self._buffers:
            buffer = self._buffers[0]
            buffer_len = len(buffer)
            if buffer_len - self._first_pos <= size:
                size -= buffer_len - self._first_pos
                self._first_pos = 0
                self._buffers.popleft()
            else:
                self._first_pos += size
                size = 0

class BaseIOStream(object):
    """A utility class to write to and read from a non-blocking file or socket.

    We support a non-blocking ``write()`` and a family of ``read_*()``
    methods. When the operation completes, the ``Awaitable`` will resolve
    with the data read (or ``None`` for ``write()``). All outstanding
    ``Awaitables`` will resolve with a `StreamClosedError` when the
    stream is closed; `.BaseIOStream.set_close_callback` can also be used
    to be notified of a closed stream.

    When a stream is closed due to an error, the IOStream's ``error``
    attribute contains the exception object.

    Subclasses must implement `fileno`, `close_fd`, `write_to_fd`,
    `read_from_fd`, and optionally `get_fd_error`.

    """

    def __init__(self, max_buffer_size: Optional[int]=None, read_chunk_size: Optional[int]=None, max_write_buffer_size: Optional[int]=None) -> None:
        """`BaseIOStream` constructor.

        :arg max_buffer_size: Maximum amount of incoming data to buffer;
            defaults to 100MB.
        :arg read_chunk_size: Amount of data to read at one time from the
            underlying transport; defaults to 64KB.
        :arg max_write_buffer_size: Amount of outgoing data to buffer;
            defaults to unlimited.

        .. versionchanged:: 4.0
           Add the ``max_write_buffer_size`` parameter.  Changed default
           ``read_chunk_size`` to 64KB.
        .. versionchanged:: 5.0
           The ``io_loop`` argument (deprecated since version 4.1) has been
           removed.
        """
        self.io_loop = ioloop.IOLoop.current()
        self.max_buffer_size = max_buffer_size or 104857600
        self.read_chunk_size = min(read_chunk_size or 65536, self.max_buffer_size // 2)
        self.max_write_buffer_size = max_write_buffer_size
        self.error = None
        self._read_buffer = bytearray()
        self._read_buffer_size = 0
        self._user_read_buffer = False
        self._after_user_read_buffer = None
        self._write_buffer = _StreamBuffer()
        self._total_write_index = 0
        self._total_write_done_index = 0
        self._read_delimiter = None
        self._read_regex = None
        self._read_max_bytes = None
        self._read_bytes = None
        self._read_partial = False
        self._read_until_close = False
        self._read_future = None
        self._write_futures = collections.deque()
        self._close_callback = None
        self._connect_future = None
        self._ssl_connect_future = None
        self._connecting = False
        self._state = None
        self._closed = False

    def fileno(self) -> Union[int, ioloop._Selectable]:
        """Returns the file descriptor for this stream."""
        return self.socket.fileno()

    def close_fd(self) -> None:
        """Closes the file underlying this stream.

        ``close_fd`` is called by `BaseIOStream` and should not be called
        elsewhere; other users should call `close` instead.
        """
        try:
            self.socket.close()
        except Exception:
            gen_log.error("error closing socket", exc_info=True)

    def write_to_fd(self, data: memoryview) -> int:
        """Attempts to write ``data`` to the underlying file.

        Returns the number of bytes written.
        """
        pass

    def read_from_fd(self, buf: Union[bytearray, memoryview]) -> Optional[int]:
        """Attempts to read from the underlying file.

        Reads up to ``len(buf)`` bytes, storing them in the buffer.
        Returns the number of bytes read. Returns None if there was
        nothing to read (the socket returned `~errno.EWOULDBLOCK` or
        equivalent), and zero on EOF.

        .. versionchanged:: 5.0

           Interface redesigned to take a buffer and return a number
           of bytes instead of a freshly-allocated object.
        """
        pass

    def get_fd_error(self) -> Optional[Exception]:
        """Returns information about any error on the underlying file.

        This method is called after the `.IOLoop` has signaled an error on the
        file descriptor, and should return an Exception (such as `socket.error`
        with additional information, or None if no such information is
        available.
        """
        pass

    def read_until_regex(self, regex: bytes, max_bytes: Optional[int]=None) -> Awaitable[bytes]:
        """Asynchronously read until we have matched the given regex.

        The result includes the data that matches the regex and anything
        that came before it.

        If ``max_bytes`` is not None, the connection will be closed
        if more than ``max_bytes`` bytes have been read and the regex is
        not satisfied.

        .. versionchanged:: 4.0
            Added the ``max_bytes`` argument.  The ``callback`` argument is
            now optional and a `.Future` will be returned if it is omitted.

        .. versionchanged:: 6.0

           The ``callback`` argument was removed. Use the returned
           `.Future` instead.

        """
        pass

    def read_until(self, delimiter: bytes, max_bytes: Optional[int]=None) -> Awaitable[bytes]:
        """Asynchronously read until we have found the given delimiter.

        The result includes all the data read including the delimiter.

        If ``max_bytes`` is not None, the connection will be closed
        if more than ``max_bytes`` bytes have been read and the delimiter
        is not found.

        .. versionchanged:: 4.0
            Added the ``max_bytes`` argument.  The ``callback`` argument is
            now optional and a `.Future` will be returned if it is omitted.

        .. versionchanged:: 6.0

           The ``callback`` argument was removed. Use the returned
           `.Future` instead.
        """
        pass

    def read_bytes(self, num_bytes: int, partial: bool=False) -> Awaitable[bytes]:
        """Asynchronously read a number of bytes.

        If ``partial`` is true, data is returned as soon as we have
        any bytes to return (but never more than ``num_bytes``)

        .. versionchanged:: 4.0
            Added the ``partial`` argument.  The callback argument is now
            optional and a `.Future` will be returned if it is omitted.

        .. versionchanged:: 6.0

           The ``callback`` and ``streaming_callback`` arguments have
           been removed. Use the returned `.Future` (and
           ``partial=True`` for ``streaming_callback``) instead.

        """
        pass

    def read_into(self, buf: bytearray, partial: bool=False) -> Awaitable[int]:
        """Asynchronously read a number of bytes.

        ``buf`` must be a writable buffer into which data will be read.

        If ``partial`` is true, the callback is run as soon as any bytes
        have been read.  Otherwise, it is run when the ``buf`` has been
        entirely filled with read data.

        .. versionadded:: 5.0

        .. versionchanged:: 6.0

           The ``callback`` argument was removed. Use the returned
           `.Future` instead.

        """
        pass

    def read_until_close(self) -> Awaitable[bytes]:
        """Asynchronously reads all data from the socket until it is closed.

        This will buffer all available data until ``max_buffer_size``
        is reached. If flow control or cancellation are desired, use a
        loop with `read_bytes(partial=True) <.read_bytes>` instead.

        .. versionchanged:: 4.0
            The callback argument is now optional and a `.Future` will
            be returned if it is omitted.

        .. versionchanged:: 6.0

           The ``callback`` and ``streaming_callback`` arguments have
           been removed. Use the returned `.Future` (and `read_bytes`
           with ``partial=True`` for ``streaming_callback``) instead.

        """
        pass

    def write(self, data: Union[bytes, memoryview]) -> 'Future[None]':
        """Asynchronously write the given data to this stream.

        This method returns a `.Future` that resolves (with a result
        of ``None``) when the write has been completed.

        The ``data`` argument may be of type `bytes` or `memoryview`.

        .. versionchanged:: 4.0
            Now returns a `.Future` if no callback is given.

        .. versionchanged:: 4.5
            Added support for `memoryview` arguments.

        .. versionchanged:: 6.0

           The ``callback`` argument was removed. Use the returned
           `.Future` instead.

        """
        pass

    def set_close_callback(self, callback: Optional[Callable[[], None]]) -> None:
        """Call the given callback when the stream is closed.

        This mostly is not necessary for applications that use the
        `.Future` interface; all outstanding ``Futures`` will resolve
        with a `StreamClosedError` when the stream is closed. However,
        it is still useful as a way to signal that the stream has been
        closed while no other read or write is in progress.

        Unlike other callback-based interfaces, ``set_close_callback``
        was not removed in Tornado 6.0.
        """
        pass

    def close(self, exc_info: Union[None, bool, BaseException, Tuple['Optional[Type[BaseException]]', Optional[BaseException], Optional[TracebackType]]]=False) -> None:
        """Close this stream.

        If ``exc_info`` is true, set the ``error`` attribute to the current
        exception from `sys.exc_info` (or if ``exc_info`` is a tuple,
        use that instead of `sys.exc_info`).
        """
        pass

    def reading(self) -> bool:
        """Returns ``True`` if we are currently reading from the stream."""
        pass

    def writing(self) -> bool:
        """Returns ``True`` if we are currently writing to the stream."""
        pass

    def closed(self) -> bool:
        """Returns ``True`` if the stream has been closed."""
        pass

    def set_nodelay(self, value: bool) -> None:
        """Sets the no-delay flag for this stream.

        By default, data written to TCP streams may be held for a time
        to make the most efficient use of bandwidth (according to
        Nagle's algorithm).  The no-delay flag requests that data be
        written as soon as possible, even if doing so would consume
        additional bandwidth.

        This flag is currently defined only for TCP-based ``IOStreams``.

        .. versionadded:: 3.1
        """
        pass

    def _try_inline_read(self) -> None:
        """Attempt to complete the current read operation from buffered data.

        If the read can be completed without blocking, schedules the
        read callback on the next IOLoop iteration; otherwise starts
        listening for reads on the socket.
        """
        pass

    def _read_to_buffer(self) -> Optional[int]:
        """Reads from the socket and appends the result to the read buffer.

        Returns the number of bytes read.  Returns 0 if there is nothing
        to read (i.e. the read returns EWOULDBLOCK or equivalent).  On
        error closes the socket and raises an exception.
        """
        pass

    def _read_from_buffer(self, pos: int) -> None:
        """Attempts to complete the currently-pending read from the buffer.

        The argument is either a position in the read buffer or None,
        as returned by _find_read_pos.
        """
        pass

    def _find_read_pos(self) -> Optional[int]:
        """Attempts to find a position in the read buffer that satisfies
        the currently-pending read.

        Returns a position in the buffer if the current read can be satisfied,
        or None if it cannot.
        """
        pass

    def _add_io_state(self, state: int) -> None:
        """Adds `state` (IOLoop.{READ,WRITE} flags) to our event handler.

        Implementation notes: Reads and writes have a fast path and a
        slow path.  The fast path reads synchronously from socket
        buffers, while the slow path uses `_add_io_state` to schedule
        an IOLoop callback.

        To detect closed connections, we must have called
        `_add_io_state` at some point, but we want to delay this as
        much as possible so we don't have to set an `IOLoop.ERROR`
        listener that will be overwritten by the next slow-path
        operation. If a sequence of fast-path ops do not end in a
        slow-path op, (e.g. for an @asynchronous long-poll request),
        we must add the error handler.

        TODO: reevaluate this now that callbacks are gone.

        """
        pass

    def _is_connreset(self, exc: BaseException) -> bool:
        """Return ``True`` if exc is ECONNRESET or equivalent.

        May be overridden in subclasses.
        """
        pass

class IOStream(BaseIOStream):
    """Socket-based `IOStream` implementation.

    This class supports the read and write methods from `BaseIOStream`
    plus a `connect` method.

    The ``socket`` parameter may either be connected or unconnected.
    For server operations the socket is the result of calling
    `socket.accept <socket.socket.accept>`.  For client operations the
    socket is created with `socket.socket`, and may either be
    connected before passing it to the `IOStream` or connected with
    `IOStream.connect`.

    A very simple (and broken) HTTP client using this class:

    .. testcode::

        import socket
        import tornado

        async def main():
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM, 0)
            stream = tornado.iostream.IOStream(s)
            await stream.connect(("friendfeed.com", 80))
            await stream.write(b"GET / HTTP/1.0\\r\\nHost: friendfeed.com\\r\\n\\r\\n")
            header_data = await stream.read_until(b"\\r\\n\\r\\n")
            headers = {}
            for line in header_data.split(b"\\r\\n"):
                parts = line.split(b":")
                if len(parts) == 2:
                    headers[parts[0].strip()] = parts[1].strip()
            body_data = await stream.read_bytes(int(headers[b"Content-Length"]))
            print(body_data)
            stream.close()

        if __name__ == '__main__':
            asyncio.run(main())

    .. testoutput::
       :hide:

    """

    def __init__(self, socket: socket.socket, *args: Any, **kwargs: Any) -> None:
        self.socket = socket
        self.socket.setblocking(False)
        super().__init__(*args, **kwargs)

    def connect(self: _IOStreamType, address: Any, server_hostname: Optional[str]=None) -> 'Future[_IOStreamType]':
        """Connects the socket to a remote address without blocking.

        May only be called if the socket passed to the constructor was
        not previously connected.  The address parameter is in the
        same format as for `socket.connect <socket.socket.connect>` for
        the type of socket passed to the IOStream constructor,
        e.g. an ``(ip, port)`` tuple.  Hostnames are accepted here,
        but will be resolved synchronously and block the IOLoop.
        If you have a hostname instead of an IP address, the `.TCPClient`
        class is recommended instead of calling this method directly.
        `.TCPClient` will do asynchronous DNS resolution and handle
        both IPv4 and IPv6.

        If ``callback`` is specified, it will be called with no
        arguments when the connection is completed; if not this method
        returns a `.Future` (whose result after a successful
        connection will be the stream itself).

        In SSL mode, the ``server_hostname`` parameter will be used
        for certificate validation (unless disabled in the
        ``ssl_options``) and SNI (if supported; requires Python
        2.7.9+).

        Note that it is safe to call `IOStream.write
        <BaseIOStream.write>` while the connection is pending, in
        which case the data will be written as soon as the connection
        is ready.  Calling `IOStream` read methods before the socket is
        connected works on some platforms but is non-portable.

        .. versionchanged:: 4.0
            If no callback is given, returns a `.Future`.

        .. versionchanged:: 4.2
           SSL certificates are validated by default; pass
           ``ssl_options=dict(cert_reqs=ssl.CERT_NONE)`` or a
           suitably-configured `ssl.SSLContext` to the
           `SSLIOStream` constructor to disable.

        .. versionchanged:: 6.0

           The ``callback`` argument was removed. Use the returned
           `.Future` instead.

        """
        pass

    def start_tls(self, server_side: bool, ssl_options: Optional[Union[Dict[str, Any], ssl.SSLContext]]=None, server_hostname: Optional[str]=None) -> Awaitable['SSLIOStream']:
        """Convert this `IOStream` to an `SSLIOStream`.

        This enables protocols that begin in clear-text mode and
        switch to SSL after some initial negotiation (such as the
        ``STARTTLS`` extension to SMTP and IMAP).

        This method cannot be used if there are outstanding reads
        or writes on the stream, or if there is any data in the
        IOStream's buffer (data in the operating system's socket
        buffer is allowed).  This means it must generally be used
        immediately after reading or writing the last clear-text
        data.  It can also be used immediately after connecting,
        before any reads or writes.

        The ``ssl_options`` argument may be either an `ssl.SSLContext`
        object or a dictionary of keyword arguments for the
        `ssl.SSLContext.wrap_socket` function.  The ``server_hostname`` argument
        will be used for certificate validation unless disabled
        in the ``ssl_options``.

        This method returns a `.Future` whose result is the new
        `SSLIOStream`.  After this method has been called,
        any other operation on the original stream is undefined.

        If a close callback is defined on this stream, it will be
        transferred to the new stream.

        .. versionadded:: 4.0

        .. versionchanged:: 4.2
           SSL certificates are validated by default; pass
           ``ssl_options=dict(cert_reqs=ssl.CERT_NONE)`` or a
           suitably-configured `ssl.SSLContext` to disable.
        """
        pass

class SSLIOStream(IOStream):
    """A utility class to write to and read from a non-blocking SSL socket.

    If the socket passed to the constructor is already connected,
    it should be wrapped with::

        ssl.SSLContext(...).wrap_socket(sock, do_handshake_on_connect=False, **kwargs)

    before constructing the `SSLIOStream`.  Unconnected sockets will be
    wrapped when `IOStream.connect` is finished.
    """
    socket = None

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """The ``ssl_options`` keyword argument may either be an
        `ssl.SSLContext` object or a dictionary of keywords arguments
        for `ssl.SSLContext.wrap_socket`
        """
        self._ssl_options = kwargs.pop('ssl_options', _client_ssl_defaults)
        super().__init__(*args, **kwargs)
        self._ssl_accepting = True
        self._handshake_reading = False
        self._handshake_writing = False
        self._server_hostname = None
        try:
            self.socket.getpeername()
        except socket.error:
            pass
        else:
            self._add_io_state(self.io_loop.WRITE)

    def wait_for_handshake(self) -> 'Future[SSLIOStream]':
        """Wait for the initial SSL handshake to complete.

        If a ``callback`` is given, it will be called with no
        arguments once the handshake is complete; otherwise this
        method returns a `.Future` which will resolve to the
        stream itself after the handshake is complete.

        Once the handshake is complete, information such as
        the peer's certificate and NPN/ALPN selections may be
        accessed on ``self.socket``.

        This method is intended for use on server-side streams
        or after using `IOStream.start_tls`; it should not be used
        with `IOStream.connect` (which already waits for the
        handshake to complete). It may only be called once per stream.

        .. versionadded:: 4.2

        .. versionchanged:: 6.0

           The ``callback`` argument was removed. Use the returned
           `.Future` instead.

        """
        pass

class PipeIOStream(BaseIOStream):
    """Pipe-based `IOStream` implementation.

    The constructor takes an integer file descriptor (such as one returned
    by `os.pipe`) rather than an open file object.  Pipes are generally
    one-way, so a `PipeIOStream` can be used for reading or writing but not
    both.

    ``PipeIOStream`` is only available on Unix-based platforms.
    """

    def __init__(self, fd: int, *args: Any, **kwargs: Any) -> None:
        self.fd = fd
        self._fio = io.FileIO(self.fd, 'r+')
        if sys.platform == 'win32':
            raise AssertionError('PipeIOStream is not supported on Windows')
        os.set_blocking(fd, False)
        super().__init__(*args, **kwargs)
