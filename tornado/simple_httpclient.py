from tornado.escape import _unicode
from tornado import gen, version
from tornado.httpclient import HTTPResponse, HTTPError, AsyncHTTPClient, main, _RequestProxy, HTTPRequest
from tornado import httputil
from tornado.http1connection import HTTP1Connection, HTTP1ConnectionParameters
from tornado.ioloop import IOLoop
from tornado.iostream import StreamClosedError, IOStream
from tornado.netutil import Resolver, OverrideResolver, _client_ssl_defaults, is_valid_ip
from tornado.log import gen_log
from tornado.tcpclient import TCPClient
import base64
import collections
import copy
import functools
import re
import socket
import ssl
import sys
import time
from io import BytesIO
import urllib.parse
from typing import Dict, Any, Callable, Optional, Type, Union
from types import TracebackType
import typing
if typing.TYPE_CHECKING:
    from typing import Deque, Tuple, List

class HTTPTimeoutError(HTTPError):
    """Error raised by SimpleAsyncHTTPClient on timeout.

    For historical reasons, this is a subclass of `.HTTPClientError`
    which simulates a response code of 599.

    .. versionadded:: 5.1
    """

    def __init__(self, message: str) -> None:
        super().__init__(599, message=message)

    def __str__(self) -> str:
        return self.message or f'HTTP {self.code}: Timeout'

class HTTPStreamClosedError(HTTPError):
    """Error raised by SimpleAsyncHTTPClient when the underlying stream is closed.

    When a more specific exception is available (such as `ConnectionResetError`),
    it may be raised instead of this one.

    For historical reasons, this is a subclass of `.HTTPClientError`
    which simulates a response code of 599.

    .. versionadded:: 5.1
    """

    def __init__(self, message: str) -> None:
        super().__init__(599, message=message)

    def __str__(self) -> str:
        return self.message or 'Stream closed'

class SimpleAsyncHTTPClient(AsyncHTTPClient):
    """Non-blocking HTTP client with no external dependencies.

    This class implements an HTTP 1.1 client on top of Tornado's IOStreams.
    Some features found in the curl-based AsyncHTTPClient are not yet
    supported.  In particular, proxies are not supported, connections
    are not reused, and callers cannot select the network interface to be
    used.

    This implementation supports the following arguments, which can be passed
    to ``configure()`` to control the global singleton, or to the constructor
    when ``force_instance=True``.

    ``max_clients`` is the number of concurrent requests that can be
    in progress; when this limit is reached additional requests will be
    queued. Note that time spent waiting in this queue still counts
    against the ``request_timeout``.

    ``defaults`` is a dict of parameters that will be used as defaults on all
    `.HTTPRequest` objects submitted to this client.

    ``hostname_mapping`` is a dictionary mapping hostnames to IP addresses.
    It can be used to make local DNS changes when modifying system-wide
    settings like ``/etc/hosts`` is not possible or desirable (e.g. in
    unittests). ``resolver`` is similar, but using the `.Resolver` interface
    instead of a simple mapping.

    ``max_buffer_size`` (default 100MB) is the number of bytes
    that can be read into memory at once. ``max_body_size``
    (defaults to ``max_buffer_size``) is the largest response body
    that the client will accept.  Without a
    ``streaming_callback``, the smaller of these two limits
    applies; with a ``streaming_callback`` only ``max_body_size``
    does.

    .. versionchanged:: 4.2
        Added the ``max_body_size`` argument.
    """

    def _on_timeout(self, key: object, info: Optional[str]=None) -> None:
        """Timeout callback of request.

        Construct a timeout HTTPResponse when a timeout occurs.

        :arg object key: A simple object to mark the request.
        :info string key: More detailed timeout information.
        """
        request = self.active.get(key)
        if request is None:
            return

        self.active.pop(key)
        timeout_response = HTTPResponse(
            request, 599, error=HTTPTimeoutError("Timeout"),
            request_time=self.io_loop.time() - request.start_time)
        if info:
            timeout_response.error.info = info

        self.handle_response(request, timeout_response)

class _HTTPConnection(httputil.HTTPMessageDelegate):
    _SUPPORTED_METHODS = set(['GET', 'HEAD', 'POST', 'PUT', 'DELETE', 'PATCH', 'OPTIONS'])

    def __init__(self, client: Optional[SimpleAsyncHTTPClient], request: HTTPRequest, release_callback: Callable[[], None], final_callback: Callable[[HTTPResponse], None], max_buffer_size: int, tcp_client: TCPClient, max_header_size: int, max_body_size: int) -> None:
        self.io_loop = IOLoop.current()
        self.start_time = self.io_loop.time()
        self.start_wall_time = time.time()
        self.client = client
        self.request = request
        self.release_callback = release_callback
        self.final_callback = final_callback
        self.max_buffer_size = max_buffer_size
        self.tcp_client = tcp_client
        self.max_header_size = max_header_size
        self.max_body_size = max_body_size
        self.code = None
        self.headers = None
        self.chunks = []
        self._decompressor = None
        self._timeout = None
        self._sockaddr = None
        IOLoop.current().add_future(gen.convert_yielded(self.run()), lambda f: f.result())

    def _on_timeout(self, info: Optional[str]=None) -> None:
        """Timeout callback of _HTTPConnection instance.

        Raise a `HTTPTimeoutError` when a timeout occurs.

        :info string key: More detailed timeout information.
        """
        error_message = "Timeout"
        if info:
            error_message += f": {info}"
        
        self.error = HTTPTimeoutError(error_message)
        if self.final_callback is not None:
            self.final_callback(self._format_error_response())
        
        if self.release_callback is not None:
            self.release_callback()
        
        self._close_stream()
if __name__ == '__main__':
    AsyncHTTPClient.configure(SimpleAsyncHTTPClient)
    main()
