"""A non-blocking, single-threaded HTTP server.

Typical applications have little direct interaction with the `HTTPServer`
class except to start a server at the beginning of the process
(and even that is often done indirectly via `tornado.web.Application.listen`).

.. versionchanged:: 4.0

   The ``HTTPRequest`` class that used to live in this module has been moved
   to `tornado.httputil.HTTPServerRequest`.  The old name remains as an alias.
"""
import socket
import ssl
from tornado.escape import native_str
from tornado.http1connection import HTTP1ServerConnection, HTTP1ConnectionParameters
from tornado import httputil
from tornado import iostream
from tornado import netutil
from tornado.tcpserver import TCPServer
from tornado.util import Configurable
import typing
from typing import Union, Any, Dict, Callable, List, Type, Tuple, Optional, Awaitable
if typing.TYPE_CHECKING:
    from typing import Set

class HTTPServer(TCPServer, Configurable, httputil.HTTPServerConnectionDelegate):
    """A non-blocking, single-threaded HTTP server.

    A server is defined by a subclass of `.HTTPServerConnectionDelegate`,
    or, for backwards compatibility, a callback that takes an
    `.HTTPServerRequest` as an argument. The delegate is usually a
    `tornado.web.Application`.

    `HTTPServer` supports keep-alive connections by default
    (automatically for HTTP/1.1, or for HTTP/1.0 when the client
    requests ``Connection: keep-alive``).

    If ``xheaders`` is ``True``, we support the
    ``X-Real-Ip``/``X-Forwarded-For`` and
    ``X-Scheme``/``X-Forwarded-Proto`` headers, which override the
    remote IP and URI scheme/protocol for all requests.  These headers
    are useful when running Tornado behind a reverse proxy or load
    balancer.  The ``protocol`` argument can also be set to ``https``
    if Tornado is run behind an SSL-decoding proxy that does not set one of
    the supported ``xheaders``.

    By default, when parsing the ``X-Forwarded-For`` header, Tornado will
    select the last (i.e., the closest) address on the list of hosts as the
    remote host IP address.  To select the next server in the chain, a list of
    trusted downstream hosts may be passed as the ``trusted_downstream``
    argument.  These hosts will be skipped when parsing the ``X-Forwarded-For``
    header.

    To make this server serve SSL traffic, send the ``ssl_options`` keyword
    argument with an `ssl.SSLContext` object. For compatibility with older
    versions of Python ``ssl_options`` may also be a dictionary of keyword
    arguments for the `ssl.SSLContext.wrap_socket` method.::

       ssl_ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
       ssl_ctx.load_cert_chain(os.path.join(data_dir, "mydomain.crt"),
                               os.path.join(data_dir, "mydomain.key"))
       HTTPServer(application, ssl_options=ssl_ctx)

    `HTTPServer` initialization follows one of three patterns (the
    initialization methods are defined on `tornado.tcpserver.TCPServer`):

    1. `~tornado.tcpserver.TCPServer.listen`: single-process::

            async def main():
                server = HTTPServer()
                server.listen(8888)
                await asyncio.Event.wait()

            asyncio.run(main())

       In many cases, `tornado.web.Application.listen` can be used to avoid
       the need to explicitly create the `HTTPServer`.

       While this example does not create multiple processes on its own, when
       the ``reuse_port=True`` argument is passed to ``listen()`` you can run
       the program multiple times to create a multi-process service.

    2. `~tornado.tcpserver.TCPServer.add_sockets`: multi-process::

            sockets = bind_sockets(8888)
            tornado.process.fork_processes(0)
            async def post_fork_main():
                server = HTTPServer()
                server.add_sockets(sockets)
                await asyncio.Event().wait()
            asyncio.run(post_fork_main())

       The ``add_sockets`` interface is more complicated, but it can be used with
       `tornado.process.fork_processes` to run a multi-process service with all
       worker processes forked from a single parent.  ``add_sockets`` can also be
       used in single-process servers if you want to create your listening
       sockets in some way other than `~tornado.netutil.bind_sockets`.

       Note that when using this pattern, nothing that touches the event loop
       can be run before ``fork_processes``.

    3. `~tornado.tcpserver.TCPServer.bind`/`~tornado.tcpserver.TCPServer.start`:
       simple **deprecated** multi-process::

            server = HTTPServer()
            server.bind(8888)
            server.start(0)  # Forks multiple sub-processes
            IOLoop.current().start()

       This pattern is deprecated because it requires interfaces in the
       `asyncio` module that have been deprecated since Python 3.10. Support for
       creating multiple processes in the ``start`` method will be removed in a
       future version of Tornado.

    .. versionchanged:: 4.0
       Added ``decompress_request``, ``chunk_size``, ``max_header_size``,
       ``idle_connection_timeout``, ``body_timeout``, ``max_body_size``
       arguments.  Added support for `.HTTPServerConnectionDelegate`
       instances as ``request_callback``.

    .. versionchanged:: 4.1
       `.HTTPServerConnectionDelegate.start_request` is now called with
       two arguments ``(server_conn, request_conn)`` (in accordance with the
       documentation) instead of one ``(request_conn)``.

    .. versionchanged:: 4.2
       `HTTPServer` is now a subclass of `tornado.util.Configurable`.

    .. versionchanged:: 4.5
       Added the ``trusted_downstream`` argument.

    .. versionchanged:: 5.0
       The ``io_loop`` argument has been removed.
    """

    def __init__(self, request_callback, no_keep_alive=False, xheaders=False,
                 ssl_options=None, protocol=None, decompress_request=False,
                 chunk_size=None, max_header_size=None, idle_connection_timeout=None,
                 body_timeout=None, max_body_size=None, max_buffer_size=None,
                 trusted_downstream=None):
        """Initialize the HTTP Server.

        Args:
            request_callback: An instance of `tornado.web.Application` or other 
                              `HTTPServerConnectionDelegate`.
            no_keep_alive (bool): If True, always close the connection after one request.
            xheaders (bool): If True, support X-Real-Ip/X-Forwarded-For headers.
            ssl_options: SSL configuration options
            protocol: Specify the HTTP protocol version to use.
            decompress_request (bool): If True, decompress request bodies.
            chunk_size (int): Max size of chunks when reading/writing streams.
            max_header_size (int): Max size of HTTP headers.
            idle_connection_timeout (float): Timeout for idle connections.
            body_timeout (float): Timeout for reading HTTP body.
            max_body_size (int): Max size of HTTP request body.
            max_buffer_size (int): Max size of read buffer.
            trusted_downstream (list): List of trusted downstream hosts.
        """
        self.request_callback = request_callback
        self.no_keep_alive = no_keep_alive
        self.xheaders = xheaders
        self.ssl_options = ssl_options
        self.protocol = protocol
        self.decompress_request = decompress_request
        self.chunk_size = chunk_size
        self.max_header_size = max_header_size
        self.idle_connection_timeout = idle_connection_timeout
        self.body_timeout = body_timeout
        self.max_body_size = max_body_size
        self.max_buffer_size = max_buffer_size
        self.trusted_downstream = trusted_downstream
        self._connections = set()

    async def close_all_connections(self) -> None:
        """Close all open connections and asynchronously wait for them to finish.

        This method is used in combination with `~.TCPServer.stop` to
        support clean shutdowns (especially for unittests). Typical
        usage would call ``stop()`` first to stop accepting new
        connections, then ``await close_all_connections()`` to wait for
        existing connections to finish.

        This method does not currently close open websocket connections.

        Note that this method is a coroutine and must be called with ``await``.
        """
        while self._connections:
            # Use a list to avoid modification during iteration
            for conn in list(self._connections):
                await conn.close()

class _CallableAdapter(httputil.HTTPMessageDelegate):

    def __init__(self, request_callback: Callable[[httputil.HTTPServerRequest], None], request_conn: httputil.HTTPConnection) -> None:
        self.connection = request_conn
        self.request_callback = request_callback
        self.request = None
        self.delegate = None
        self._chunks = []

class _HTTPRequestContext(object):

    def __init__(self, stream: iostream.IOStream, address: Tuple, protocol: Optional[str], trusted_downstream: Optional[List[str]]=None) -> None:
        self.address = address
        if stream.socket is not None:
            self.address_family = stream.socket.family
        else:
            self.address_family = None
        if self.address_family in (socket.AF_INET, socket.AF_INET6) and address is not None:
            self.remote_ip = address[0]
        else:
            self.remote_ip = '0.0.0.0'
        if protocol:
            self.protocol = protocol
        elif isinstance(stream, iostream.SSLIOStream):
            self.protocol = 'https'
        else:
            self.protocol = 'http'
        self._orig_remote_ip = self.remote_ip
        self._orig_protocol = self.protocol
        self.trusted_downstream = set(trusted_downstream or [])

    def __str__(self) -> str:
        if self.address_family in (socket.AF_INET, socket.AF_INET6):
            return self.remote_ip
        elif isinstance(self.address, bytes):
            return native_str(self.address)
        else:
            return str(self.address)

    def _apply_xheaders(self, headers: httputil.HTTPHeaders) -> None:
        """Rewrite the ``remote_ip`` and ``protocol`` fields based on headers.

        This method is called when ``xheaders`` is set to True.

        Args:
            headers (httputil.HTTPHeaders): The request headers.
        """
        # Check for X-Real-Ip header
        real_ip = headers.get("X-Real-Ip")
        if real_ip:
            self.remote_ip = real_ip

        # Check for X-Forwarded-For header
        forward_for = headers.get("X-Forwarded-For")
        if forward_for:
            # Get the last address in the chain if there are multiple
            self.remote_ip = forward_for.split(',')[-1].strip()

        # Check for X-Scheme or X-Forwarded-Proto header
        scheme = headers.get("X-Scheme") or headers.get("X-Forwarded-Proto")
        if scheme:
            self.protocol = scheme.lower()

    def _unapply_xheaders(self) -> None:
        """Undo changes from `_apply_xheaders`.

        Xheaders are per-request so they should not leak to the next
        request on the same connection.
        """
        self.remote_ip = self._orig_remote_ip
        self.protocol = self._orig_protocol

class _ProxyAdapter(httputil.HTTPMessageDelegate):

    def __init__(self, delegate: httputil.HTTPMessageDelegate, request_conn: httputil.HTTPConnection) -> None:
        self.connection = request_conn
        self.delegate = delegate
HTTPRequest = httputil.HTTPServerRequest
def main():
    """
    Main entry point for the HTTP server.
    This function sets up and starts the server.
    """
    import asyncio
    from tornado.web import Application
    from tornado.ioloop import IOLoop

    def make_app():
        return Application([
            # Add your handlers here
        ])

    app = make_app()
    app.listen(8888)
    print("Server is running on http://localhost:8888")
    IOLoop.current().start()

if __name__ == "__main__":
    main()
