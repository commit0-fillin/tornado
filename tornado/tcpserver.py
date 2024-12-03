"""A non-blocking, single-threaded TCP server."""
import errno
import os
import socket
import ssl
from tornado import gen
from tornado.log import app_log
from tornado.ioloop import IOLoop
from tornado.iostream import IOStream, SSLIOStream
from tornado.netutil import bind_sockets, add_accept_handler, ssl_wrap_socket, _DEFAULT_BACKLOG
from tornado import process
from tornado.util import errno_from_exception
import typing
from typing import Union, Dict, Any, Iterable, Optional, Awaitable
if typing.TYPE_CHECKING:
    from typing import Callable, List

class TCPServer(object):
    """A non-blocking, single-threaded TCP server.

    To use `TCPServer`, define a subclass which overrides the `handle_stream`
    method. For example, a simple echo server could be defined like this::

      from tornado.tcpserver import TCPServer
      from tornado.iostream import StreamClosedError

      class EchoServer(TCPServer):
          async def handle_stream(self, stream, address):
              while True:
                  try:
                      data = await stream.read_until(b"\\n") await
                      stream.write(data)
                  except StreamClosedError:
                      break

    To make this server serve SSL traffic, send the ``ssl_options`` keyword
    argument with an `ssl.SSLContext` object. For compatibility with older
    versions of Python ``ssl_options`` may also be a dictionary of keyword
    arguments for the `ssl.SSLContext.wrap_socket` method.::

       ssl_ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
       ssl_ctx.load_cert_chain(os.path.join(data_dir, "mydomain.crt"),
                               os.path.join(data_dir, "mydomain.key"))
       TCPServer(ssl_options=ssl_ctx)

    `TCPServer` initialization follows one of three patterns:

    1. `listen`: single-process::

            async def main():
                server = TCPServer()
                server.listen(8888)
                await asyncio.Event.wait()

            asyncio.run(main())

       While this example does not create multiple processes on its own, when
       the ``reuse_port=True`` argument is passed to ``listen()`` you can run
       the program multiple times to create a multi-process service.

    2. `add_sockets`: multi-process::

            sockets = bind_sockets(8888)
            tornado.process.fork_processes(0)
            async def post_fork_main():
                server = TCPServer()
                server.add_sockets(sockets)
                await asyncio.Event().wait()
            asyncio.run(post_fork_main())

       The `add_sockets` interface is more complicated, but it can be used with
       `tornado.process.fork_processes` to run a multi-process service with all
       worker processes forked from a single parent.  `add_sockets` can also be
       used in single-process servers if you want to create your listening
       sockets in some way other than `~tornado.netutil.bind_sockets`.

       Note that when using this pattern, nothing that touches the event loop
       can be run before ``fork_processes``.

    3. `bind`/`start`: simple **deprecated** multi-process::

            server = TCPServer()
            server.bind(8888)
            server.start(0)  # Forks multiple sub-processes
            IOLoop.current().start()

       This pattern is deprecated because it requires interfaces in the
       `asyncio` module that have been deprecated since Python 3.10. Support for
       creating multiple processes in the ``start`` method will be removed in a
       future version of Tornado.

    .. versionadded:: 3.1
       The ``max_buffer_size`` argument.

    .. versionchanged:: 5.0
       The ``io_loop`` argument has been removed.
    """

    def __init__(self, ssl_options: Optional[Union[Dict[str, Any], ssl.SSLContext]]=None, max_buffer_size: Optional[int]=None, read_chunk_size: Optional[int]=None) -> None:
        self.ssl_options = ssl_options
        self._sockets = {}
        self._handlers = {}
        self._pending_sockets = []
        self._started = False
        self._stopped = False
        self.max_buffer_size = max_buffer_size
        self.read_chunk_size = read_chunk_size
        if self.ssl_options is not None and isinstance(self.ssl_options, dict):
            if 'certfile' not in self.ssl_options:
                raise KeyError('missing key "certfile" in ssl_options')
            if not os.path.exists(self.ssl_options['certfile']):
                raise ValueError('certfile "%s" does not exist' % self.ssl_options['certfile'])
            if 'keyfile' in self.ssl_options and (not os.path.exists(self.ssl_options['keyfile'])):
                raise ValueError('keyfile "%s" does not exist' % self.ssl_options['keyfile'])

    def listen(self, port: int, address: Optional[str] = None) -> None:
        """
        Start accepting connections on the given port.

        This method may be called more than once to listen on multiple ports.
        `listen()` takes effect immediately; it is not necessary to call
        `TCPServer.start` afterwards.  It is, however, necessary to start
        the `.IOLoop`.
        """
        sockets = bind_sockets(port, address)
        self.add_sockets(sockets)

    def add_sockets(self, sockets: List[socket.socket]) -> None:
        """
        Makes this server start accepting connections on the given sockets.

        The ``sockets`` parameter is a list of socket objects such as
        those returned by `~tornado.netutil.bind_sockets`.
        """
        for sock in sockets:
            self._sockets[sock.fileno()] = sock
            self._handlers[sock.fileno()] = add_accept_handler(
                sock, self._handle_connection)

    def add_socket(self, socket: socket.socket) -> None:
        """
        Singular version of `add_sockets`.  Takes a single socket object.
        """
        self.add_sockets([socket])

    def bind(self, port: int, address: Optional[str]=None, family: socket.AddressFamily=socket.AF_UNSPEC, backlog: int=_DEFAULT_BACKLOG, flags: Optional[int]=None, reuse_port: bool=False) -> None:
        """Binds this server to the given port on the given address.

        To start the server, call `start`. If you want to run this server in a
        single process, you can call `listen` as a shortcut to the sequence of
        `bind` and `start` calls.

        Address may be either an IP address or hostname.  If it's a hostname,
        the server will listen on all IP addresses associated with the name.
        Address may be an empty string or None to listen on all available
        interfaces.  Family may be set to either `socket.AF_INET` or
        `socket.AF_INET6` to restrict to IPv4 or IPv6 addresses, otherwise both
        will be used if available.

        The ``backlog`` argument has the same meaning as for `socket.listen
        <socket.socket.listen>`. The ``reuse_port`` argument has the same
        meaning as for `.bind_sockets`.

        This method may be called multiple times prior to `start` to listen on
        multiple ports or interfaces.

        .. versionchanged:: 4.4
           Added the ``reuse_port`` argument.

        .. versionchanged:: 6.2
           Added the ``flags`` argument to match `.bind_sockets`.

        .. deprecated:: 6.2
           Use either ``listen()`` or ``add_sockets()`` instead of ``bind()``
           and ``start()``.
        """
        pass

    def start(self, num_processes: Optional[int] = 1) -> None:
        """
        Starts this server in the `.IOLoop`.

        By default, we run the server in this process and do not fork any
        additional child process.

        If num_processes is ``None`` or <= 0, we detect the number of cores
        available on this machine and fork that number of child
        processes. If num_processes is given and > 1, we fork that
        specific number of sub-processes.

        Since we use processes and not threads, there is no shared memory
        between any server code.

        Note that multiple processes are not compatible with the autoreload
        module (or the ``autoreload=True`` option to `tornado.web.Application`
        which defaults to True when ``debug=True``).
        When using multiple processes, no IOLoops can be created or
        referenced until after the call to ``TCPServer.start(n)``.
        """
        assert not self._started
        self._started = True
        if self._pending_sockets:
            self.add_sockets(self._pending_sockets)
            self._pending_sockets = []

        if num_processes is None or num_processes <= 0:
            num_processes = cpu_count()
        if num_processes > 1 and ioloop.IOLoop.initialized():
            raise RuntimeError("Cannot run in multiple processes: IOLoop instance "
                               "has already been initialized. You cannot call "
                               "IOLoop.instance() before calling start()")
        if num_processes > 1:
            fork_processes(num_processes)
        else:
            assert num_processes == 1
            self._run_thread = threading.Thread(target=self._run)
            self._run_thread.start()

    def stop(self) -> None:
        """
        Stops listening for new connections.

        Requests currently in progress may still continue after the
        server is stopped.
        """
        for fd, sock in self._sockets.items():
            self.io_loop.remove_handler(fd)
            sock.close()

    def handle_stream(self, stream: IOStream, address: tuple) -> Optional[Awaitable[None]]:
        """
        Override this method to handle a new `.IOStream` from an incoming connection.

        This method may be a coroutine; if so any exceptions it raises
        asynchronously will be logged. Accepting of incoming connections
        will not be blocked by this coroutine.

        If this `TCPServer` is configured for SSL, ``handle_stream``
        may be called before the SSL handshake has completed. Use
        `.SSLIOStream.wait_for_handshake` if you need to verify the client's
        certificate or use NPN/ALPN.

        Args:
            stream: An `.IOStream` for the newly connected socket.
            address: The address of the client that has connected.

        Returns:
            Optional[Awaitable[None]]: If this method is a coroutine, it must return an awaitable.
        """
        raise NotImplementedError()
