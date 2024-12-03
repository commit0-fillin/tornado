"""WSGI support for the Tornado web framework.

WSGI is the Python standard for web servers, and allows for interoperability
between Tornado and other Python web frameworks and servers.

This module provides WSGI support via the `WSGIContainer` class, which
makes it possible to run applications using other WSGI frameworks on
the Tornado HTTP server. The reverse is not supported; the Tornado
`.Application` and `.RequestHandler` classes are designed for use with
the Tornado `.HTTPServer` and cannot be used in a generic WSGI
container.

"""
import concurrent.futures
from io import BytesIO
import tornado
import sys
from tornado.concurrent import dummy_executor
from tornado import escape
from tornado import httputil
from tornado.ioloop import IOLoop
from tornado.log import access_log
from typing import List, Tuple, Optional, Callable, Any, Dict, Text
from types import TracebackType
import typing
if typing.TYPE_CHECKING:
    from typing import Type
    from _typeshed.wsgi import WSGIApplication as WSGIAppType

class WSGIContainer(object):
    """Makes a WSGI-compatible application runnable on Tornado's HTTP server.

    .. warning::

       WSGI is a *synchronous* interface, while Tornado's concurrency model
       is based on single-threaded *asynchronous* execution.  Many of Tornado's
       distinguishing features are not available in WSGI mode, including efficient
       long-polling and websockets. The primary purpose of `WSGIContainer` is
       to support both WSGI applications and native Tornado ``RequestHandlers`` in
       a single process. WSGI-only applications are likely to be better off
       with a dedicated WSGI server such as ``gunicorn`` or ``uwsgi``.

    Wrap a WSGI application in a `WSGIContainer` to make it implement the Tornado
    `.HTTPServer` ``request_callback`` interface.  The `WSGIContainer` object can
    then be passed to classes from the `tornado.routing` module,
    `tornado.web.FallbackHandler`, or to `.HTTPServer` directly.

    This class is intended to let other frameworks (Django, Flask, etc)
    run on the Tornado HTTP server and I/O loop.

    Realistic usage will be more complicated, but the simplest possible example uses a
    hand-written WSGI application with `.HTTPServer`::

        def simple_app(environ, start_response):
            status = "200 OK"
            response_headers = [("Content-type", "text/plain")]
            start_response(status, response_headers)
            return [b"Hello world!\\n"]

        async def main():
            container = tornado.wsgi.WSGIContainer(simple_app)
            http_server = tornado.httpserver.HTTPServer(container)
            http_server.listen(8888)
            await asyncio.Event().wait()

        asyncio.run(main())

    The recommended pattern is to use the `tornado.routing` module to set up routing
    rules between your WSGI application and, typically, a `tornado.web.Application`.
    Alternatively, `tornado.web.Application` can be used as the top-level router
    and `tornado.web.FallbackHandler` can embed a `WSGIContainer` within it.

    If the ``executor`` argument is provided, the WSGI application will be executed
    on that executor. This must be an instance of `concurrent.futures.Executor`,
    typically a ``ThreadPoolExecutor`` (``ProcessPoolExecutor`` is not supported).
    If no ``executor`` is given, the application will run on the event loop thread in
    Tornado 6.3; this will change to use an internal thread pool by default in
    Tornado 7.0.

    .. warning::
       By default, the WSGI application is executed on the event loop's thread. This
       limits the server to one request at a time (per process), making it less scalable
       than most other WSGI servers. It is therefore highly recommended that you pass
       a ``ThreadPoolExecutor`` when constructing the `WSGIContainer`, after verifying
       that your application is thread-safe. The default will change to use a
       ``ThreadPoolExecutor`` in Tornado 7.0.

    .. versionadded:: 6.3
       The ``executor`` parameter.

    .. deprecated:: 6.3
       The default behavior of running the WSGI application on the event loop thread
       is deprecated and will change in Tornado 7.0 to use a thread pool by default.
    """

    def __init__(self, wsgi_application: 'WSGIAppType', executor: Optional[concurrent.futures.Executor]=None) -> None:
        self.wsgi_application = wsgi_application
        self.executor = dummy_executor if executor is None else executor

    def __call__(self, request: httputil.HTTPServerRequest) -> None:
        IOLoop.current().spawn_callback(self.handle_request, request)

    def environ(self, request: httputil.HTTPServerRequest) -> Dict[Text, Any]:
        """Converts a `tornado.httputil.HTTPServerRequest` to a WSGI environment.

        .. versionchanged:: 6.3
           No longer a static method.
        """
        hostport = request.host.split(":")
        if len(hostport) == 2:
            host = hostport[0]
            port = int(hostport[1])
        else:
            host = request.host
            port = 443 if request.protocol == "https" else 80
        environ = {
            "REQUEST_METHOD": request.method,
            "SCRIPT_NAME": "",
            "PATH_INFO": request.path,
            "QUERY_STRING": request.query,
            "REMOTE_ADDR": request.remote_ip,
            "SERVER_NAME": host,
            "SERVER_PORT": str(port),
            "SERVER_PROTOCOL": request.version,
            "wsgi.version": (1, 0),
            "wsgi.url_scheme": request.protocol,
            "wsgi.input": BytesIO(request.body),
            "wsgi.errors": sys.stderr,
            "wsgi.multithread": False,
            "wsgi.multiprocess": True,
            "wsgi.run_once": False,
        }

        if "Content-Type" in request.headers:
            environ["CONTENT_TYPE"] = request.headers.pop("Content-Type")
        if "Content-Length" in request.headers:
            environ["CONTENT_LENGTH"] = request.headers.pop("Content-Length")
        for key, value in request.headers.items():
            environ["HTTP_" + key.replace("-", "_").upper()] = value

        return environ

    @staticmethod
    def main():
        """Example usage for a simple TCP server."""
        import asyncio
        import errno
        import functools
        import socket

        from tornado.iostream import IOStream

        async def handle_connection(connection, address):
            stream = IOStream(connection)
            message = await stream.read_until_close()
            print("message from client:", message.decode().strip())

        def connection_ready(sock, fd, events):
            while True:
                try:
                    connection, address = sock.accept()
                except BlockingIOError:
                    return
                connection.setblocking(0)
                io_loop = IOLoop.current()
                io_loop.spawn_callback(handle_connection, connection, address)

        async def run_server():
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM, 0)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.setblocking(0)
            sock.bind(("", 8888))
            sock.listen(128)

            io_loop = IOLoop.current()
            callback = functools.partial(connection_ready, sock)
            io_loop.add_handler(sock.fileno(), callback, io_loop.READ)
            await asyncio.Event().wait()

        asyncio.run(run_server())
HTTPRequest = httputil.HTTPServerRequest
