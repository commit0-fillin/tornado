"""A non-blocking TCP connection factory.
"""
import functools
import socket
import numbers
import datetime
import ssl
import typing
from tornado.concurrent import Future, future_add_done_callback
from tornado.ioloop import IOLoop
from tornado.iostream import IOStream
from tornado import gen
from tornado.netutil import Resolver
from tornado.gen import TimeoutError
from typing import Any, Union, Dict, Tuple, List, Callable, Iterator, Optional
if typing.TYPE_CHECKING:
    from typing import Set
_INITIAL_CONNECT_TIMEOUT = 0.3

class _Connector(object):
    """A stateless implementation of the "Happy Eyeballs" algorithm.

    "Happy Eyeballs" is documented in RFC6555 as the recommended practice
    for when both IPv4 and IPv6 addresses are available.

    In this implementation, we partition the addresses by family, and
    make the first connection attempt to whichever address was
    returned first by ``getaddrinfo``.  If that connection fails or
    times out, we begin a connection in parallel to the first address
    of the other family.  If there are additional failures we retry
    with other addresses, keeping one connection attempt per family
    in flight at a time.

    http://tools.ietf.org/html/rfc6555

    """

    def __init__(self, addrinfo: List[Tuple], connect: Callable[[socket.AddressFamily, Tuple], Tuple[IOStream, 'Future[IOStream]']]) -> None:
        self.io_loop = IOLoop.current()
        self.connect = connect
        self.future = Future()
        self.timeout = None
        self.connect_timeout = None
        self.last_error = None
        self.remaining = len(addrinfo)
        self.primary_addrs, self.secondary_addrs = self.split(addrinfo)
        self.streams = set()

    @staticmethod
    def split(addrinfo: List[Tuple]) -> Tuple[List[Tuple[socket.AddressFamily, Tuple]], List[Tuple[socket.AddressFamily, Tuple]]]:
        """Partition the ``addrinfo`` list by address family.

        Returns two lists.  The first list contains the first entry from
        ``addrinfo`` and all others with the same family, and the
        second list contains all other addresses (normally one list will
        be AF_INET and the other AF_INET6, although non-standard resolvers
        may return additional families).
        """
        primary = []
        secondary = []
        primary_af = addrinfo[0][0]
        for af, addr in addrinfo:
            if af == primary_af:
                primary.append((af, addr))
            else:
                secondary.append((af, addr))
        return primary, secondary

class TCPClient(object):
    """A non-blocking TCP connection factory.

    .. versionchanged:: 5.0
       The ``io_loop`` argument (deprecated since version 4.1) has been removed.
    """

    def __init__(self, resolver: Optional[Resolver]=None) -> None:
        if resolver is not None:
            self.resolver = resolver
            self._own_resolver = False
        else:
            self.resolver = Resolver()
            self._own_resolver = True

    async def connect(self, host: str, port: int, af: socket.AddressFamily=socket.AF_UNSPEC, ssl_options: Optional[Union[Dict[str, Any], ssl.SSLContext]]=None, max_buffer_size: Optional[int]=None, source_ip: Optional[str]=None, source_port: Optional[int]=None, timeout: Optional[Union[float, datetime.timedelta]]=None) -> IOStream:
        """Connect to the given host and port.

        Asynchronously returns an `.IOStream` (or `.SSLIOStream` if
        ``ssl_options`` is not None).

        Using the ``source_ip`` kwarg, one can specify the source
        IP address to use when establishing the connection.
        In case the user needs to resolve and
        use a specific interface, it has to be handled outside
        of Tornado as this depends very much on the platform.

        Raises `TimeoutError` if the input future does not complete before
        ``timeout``, which may be specified in any form allowed by
        `.IOLoop.add_timeout` (i.e. a `datetime.timedelta` or an absolute time
        relative to `.IOLoop.time`)

        Similarly, when the user requires a certain source port, it can
        be specified using the ``source_port`` arg.

        .. versionchanged:: 4.5
           Added the ``source_ip`` and ``source_port`` arguments.

        .. versionchanged:: 5.0
           Added the ``timeout`` argument.
        """
        addrinfo = await self.resolver.resolve(host, port, af)
        connector = _Connector(addrinfo, self._create_stream)
        
        if timeout is not None:
            if isinstance(timeout, datetime.timedelta):
                timeout = timeout.total_seconds()
            connector.timeout = self.io_loop.time() + timeout

        stream = await connector.start()
        
        if ssl_options is not None:
            stream = await stream.start_tls(False, ssl_options=ssl_options, server_hostname=host)
        
        return stream
