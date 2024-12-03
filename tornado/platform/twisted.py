"""Bridges between the Twisted package and Tornado.
"""
import socket
import sys
import twisted.internet.abstract
import twisted.internet.asyncioreactor
from twisted.internet.defer import Deferred
from twisted.python import failure
import twisted.names.cache
import twisted.names.client
import twisted.names.hosts
import twisted.names.resolve
from tornado.concurrent import Future, future_set_exc_info
from tornado.escape import utf8
from tornado import gen
from tornado.netutil import Resolver
import typing
if typing.TYPE_CHECKING:
    from typing import Generator, Any, List, Tuple

class TwistedResolver(Resolver):
    """Twisted-based asynchronous resolver.

    This is a non-blocking and non-threaded resolver.  It is
    recommended only when threads cannot be used, since it has
    limitations compared to the standard ``getaddrinfo``-based
    `~tornado.netutil.Resolver` and
    `~tornado.netutil.DefaultExecutorResolver`.  Specifically, it returns at
    most one result, and arguments other than ``host`` and ``family``
    are ignored.  It may fail to resolve when ``family`` is not
    ``socket.AF_UNSPEC``.

    Requires Twisted 12.1 or newer.

    .. versionchanged:: 5.0
       The ``io_loop`` argument (deprecated since version 4.1) has been removed.

    .. deprecated:: 6.2
       This class is deprecated and will be removed in Tornado 7.0. Use the default
       thread-based resolver instead.
    """

def install() -> None:
    """Install ``AsyncioSelectorReactor`` as the default Twisted reactor.

    .. deprecated:: 5.1

       This function is provided for backwards compatibility; code
       that does not require compatibility with older versions of
       Tornado should use
       ``twisted.internet.asyncioreactor.install()`` directly.

    .. versionchanged:: 6.0.3

       In Tornado 5.x and before, this function installed a reactor
       based on the Tornado ``IOLoop``. When that reactor
       implementation was removed in Tornado 6.0.0, this function was
       removed as well. It was restored in Tornado 6.0.3 using the
       ``asyncio`` reactor instead.

    """
    from twisted.internet import asyncioreactor
    asyncioreactor.install()
if hasattr(gen.convert_yielded, 'register'):
