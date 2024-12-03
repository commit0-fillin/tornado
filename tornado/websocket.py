"""Implementation of the WebSocket protocol.

`WebSockets <http://dev.w3.org/html5/websockets/>`_ allow for bidirectional
communication between the browser and server. WebSockets are supported in the
current versions of all major browsers.

This module implements the final version of the WebSocket protocol as
defined in `RFC 6455 <http://tools.ietf.org/html/rfc6455>`_.

.. versionchanged:: 4.0
   Removed support for the draft 76 protocol version.
"""
import abc
import asyncio
import base64
import hashlib
import os
import sys
import struct
import tornado
from urllib.parse import urlparse
import warnings
import zlib
from tornado.concurrent import Future, future_set_result_unless_cancelled
from tornado.escape import utf8, native_str, to_unicode
from tornado import gen, httpclient, httputil
from tornado.ioloop import IOLoop, PeriodicCallback
from tornado.iostream import StreamClosedError, IOStream
from tornado.log import gen_log, app_log
from tornado.netutil import Resolver
from tornado import simple_httpclient
from tornado.queues import Queue
from tornado.tcpclient import TCPClient
from tornado.util import _websocket_mask
from typing import TYPE_CHECKING, cast, Any, Optional, Dict, Union, List, Awaitable, Callable, Tuple, Type
from types import TracebackType
if TYPE_CHECKING:
    from typing_extensions import Protocol

    class _Compressor(Protocol):
        pass

    class _Decompressor(Protocol):
        unconsumed_tail = b''

    class _WebSocketDelegate(Protocol):
        pass
_default_max_message_size = 10 * 1024 * 1024

class WebSocketError(Exception):
    pass

class WebSocketClosedError(WebSocketError):
    """Raised by operations on a closed connection.

    .. versionadded:: 3.2
    """
    pass

class _DecompressTooLargeError(Exception):
    pass

class _WebSocketParams(object):

    def __init__(self, ping_interval: Optional[float]=None, ping_timeout: Optional[float]=None, max_message_size: int=_default_max_message_size, compression_options: Optional[Dict[str, Any]]=None) -> None:
        self.ping_interval = ping_interval
        self.ping_timeout = ping_timeout
        self.max_message_size = max_message_size
        self.compression_options = compression_options

class WebSocketHandler(tornado.web.RequestHandler):
    """Subclass this class to create a basic WebSocket handler.

    Override `on_message` to handle incoming messages, and use
    `write_message` to send messages to the client. You can also
    override `open` and `on_close` to handle opened and closed
    connections.

    Custom upgrade response headers can be sent by overriding
    `~tornado.web.RequestHandler.set_default_headers` or
    `~tornado.web.RequestHandler.prepare`.

    See http://dev.w3.org/html5/websockets/ for details on the
    JavaScript interface.  The protocol is specified at
    http://tools.ietf.org/html/rfc6455.

    Here is an example WebSocket handler that echos back all received messages
    back to the client:

    .. testcode::

      class EchoWebSocket(tornado.websocket.WebSocketHandler):
          def open(self):
              print("WebSocket opened")

          def on_message(self, message):
              self.write_message(u"You said: " + message)

          def on_close(self):
              print("WebSocket closed")

    .. testoutput::
       :hide:

    WebSockets are not standard HTTP connections. The "handshake" is
    HTTP, but after the handshake, the protocol is
    message-based. Consequently, most of the Tornado HTTP facilities
    are not available in handlers of this type. The only communication
    methods available to you are `write_message()`, `ping()`, and
    `close()`. Likewise, your request handler class should implement
    `open()` method rather than ``get()`` or ``post()``.

    If you map the handler above to ``/websocket`` in your application, you can
    invoke it in JavaScript with::

      var ws = new WebSocket("ws://localhost:8888/websocket");
      ws.onopen = function() {
         ws.send("Hello, world");
      };
      ws.onmessage = function (evt) {
         alert(evt.data);
      };

    This script pops up an alert box that says "You said: Hello, world".

    Web browsers allow any site to open a websocket connection to any other,
    instead of using the same-origin policy that governs other network
    access from JavaScript.  This can be surprising and is a potential
    security hole, so since Tornado 4.0 `WebSocketHandler` requires
    applications that wish to receive cross-origin websockets to opt in
    by overriding the `~WebSocketHandler.check_origin` method (see that
    method's docs for details).  Failure to do so is the most likely
    cause of 403 errors when making a websocket connection.

    When using a secure websocket connection (``wss://``) with a self-signed
    certificate, the connection from a browser may fail because it wants
    to show the "accept this certificate" dialog but has nowhere to show it.
    You must first visit a regular HTML page using the same certificate
    to accept it before the websocket connection will succeed.

    If the application setting ``websocket_ping_interval`` has a non-zero
    value, a ping will be sent periodically, and the connection will be
    closed if a response is not received before the ``websocket_ping_timeout``.

    Messages larger than the ``websocket_max_message_size`` application setting
    (default 10MiB) will not be accepted.

    .. versionchanged:: 4.5
       Added ``websocket_ping_interval``, ``websocket_ping_timeout``, and
       ``websocket_max_message_size``.
    """

    def __init__(self, application: tornado.web.Application, request: httputil.HTTPServerRequest, **kwargs: Any) -> None:
        super().__init__(application, request, **kwargs)
        self.ws_connection = None
        self.close_code = None
        self.close_reason = None
        self._on_close_called = False

    @property
    def ping_interval(self) -> Optional[float]:
        """The interval for websocket keep-alive pings.

        Set websocket_ping_interval = 0 to disable pings.
        """
        return self.application.settings.get('websocket_ping_interval', None)

    @property
    def ping_timeout(self) -> Optional[float]:
        """If no ping is received in this many seconds,
        close the websocket connection (VPNs, etc. can fail to cleanly close ws connections).
        Default is max of 3 pings or 30 seconds.
        """
        return self.application.settings.get('websocket_ping_timeout', 
                                             max(3 * (self.ping_interval or 0), 30))

    @property
    def max_message_size(self) -> int:
        """Maximum allowed message size.

        If the remote peer sends a message larger than this, the connection
        will be closed.

        Default is 10MiB.
        """
        return self.application.settings.get('websocket_max_message_size', 10 * 1024 * 1024)

    def write_message(self, message: Union[bytes, str, Dict[str, Any]], binary: bool=False) -> 'Future[None]':
        """Sends the given message to the client of this Web Socket.

        The message may be either a string or a dict (which will be
        encoded as json).  If the ``binary`` argument is false, the
        message will be sent as utf8; in binary mode any byte string
        is allowed.

        If the connection is already closed, raises `WebSocketClosedError`.
        Returns a `.Future` which can be used for flow control.

        .. versionchanged:: 3.2
           `WebSocketClosedError` was added (previously a closed connection
           would raise an `AttributeError`)

        .. versionchanged:: 4.3
           Returns a `.Future` which can be used for flow control.

        .. versionchanged:: 5.0
           Consistently raises `WebSocketClosedError`. Previously could
           sometimes raise `.StreamClosedError`.
        """
        if self.ws_connection is None:
            raise WebSocketClosedError()
        if isinstance(message, dict):
            message = json.dumps(message)
        if isinstance(message, str):
            message = message.encode('utf-8')
        return self.ws_connection.write_message(message, binary=binary)

    def select_subprotocol(self, subprotocols: List[str]) -> Optional[str]:
        """Override to implement subprotocol negotiation.

        ``subprotocols`` is a list of strings identifying the
        subprotocols proposed by the client.  This method may be
        overridden to return one of those strings to select it, or
        ``None`` to not select a subprotocol.

        Failure to select a subprotocol does not automatically abort
        the connection, although clients may close the connection if
        none of their proposed subprotocols was selected.

        The list may be empty, in which case this method must return
        None. This method is always called exactly once even if no
        subprotocols were proposed so that the handler can be advised
        of this fact.

        .. versionchanged:: 5.1

           Previously, this method was called with a list containing
           an empty string instead of an empty list if no subprotocols
           were proposed by the client.
        """
        return None

    @property
    def selected_subprotocol(self) -> Optional[str]:
        """The subprotocol returned by `select_subprotocol`.

        .. versionadded:: 5.1
        """
        return self.ws_connection.selected_subprotocol if self.ws_connection else None

    def get_compression_options(self) -> Optional[Dict[str, Any]]:
        """Override to return compression options for the connection.

        If this method returns None (the default), compression will
        be disabled.  If it returns a dict (even an empty one), it
        will be enabled.  The contents of the dict may be used to
        control the following compression options:

        ``compression_level`` specifies the compression level.

        ``mem_level`` specifies the amount of memory used for the internal compression state.

         These parameters are documented in details here:
         https://docs.python.org/3.6/library/zlib.html#zlib.compressobj

        .. versionadded:: 4.1

        .. versionchanged:: 4.5

           Added ``compression_level`` and ``mem_level``.
        """
        return None

    def open(self, *args: str, **kwargs: str) -> Optional[Awaitable[None]]:
        """Invoked when a new WebSocket is opened.

        The arguments to `open` are extracted from the `tornado.web.URLSpec`
        regular expression, just like the arguments to
        `tornado.web.RequestHandler.get`.

        `open` may be a coroutine. `on_message` will not be called until
        `open` has returned.

        .. versionchanged:: 5.1

           ``open`` may be a coroutine.
        """
        pass

    def on_message(self, message: Union[str, bytes]) -> Optional[Awaitable[None]]:
        """Handle incoming messages on the WebSocket

        This method must be overridden.

        .. versionchanged:: 4.5

           ``on_message`` can be a coroutine.
        """
        raise NotImplementedError("on_message must be overridden")

    def ping(self, data: Union[str, bytes]=b'') -> None:
        """Send ping frame to the remote end.

        The data argument allows a small amount of data (up to 125
        bytes) to be sent as a part of the ping message. Note that not
        all websocket implementations expose this data to
        applications.

        Consider using the ``websocket_ping_interval`` application
        setting instead of sending pings manually.

        .. versionchanged:: 5.1

           The data argument is now optional.

        """
        if self.ws_connection is None:
            raise WebSocketClosedError()
        self.ws_connection.write_ping(data)

    def on_pong(self, data: bytes) -> None:
        """Invoked when the response to a ping frame is received."""
        pass

    def on_ping(self, data: bytes) -> None:
        """Invoked when the a ping frame is received."""
        pass

    def on_close(self) -> None:
        """Invoked when the WebSocket is closed.

        If the connection was closed cleanly and a status code or reason
        phrase was supplied, these values will be available as the attributes
        ``self.close_code`` and ``self.close_reason``.

        .. versionchanged:: 4.0

           Added ``close_code`` and ``close_reason`` attributes.
        """
        pass

    def close(self, code: Optional[int]=None, reason: Optional[str]=None) -> None:
        """Closes this Web Socket.

        Once the close handshake is successful the socket will be closed.

        ``code`` may be a numeric status code, taken from the values
        defined in `RFC 6455 section 7.4.1
        <https://tools.ietf.org/html/rfc6455#section-7.4.1>`_.
        ``reason`` may be a textual message about why the connection is
        closing.  These values are made available to the client, but are
        not otherwise interpreted by the websocket protocol.

        .. versionchanged:: 4.0

           Added the ``code`` and ``reason`` arguments.
        """
        if self.ws_connection:
            self.ws_connection.close(code, reason)
            self.ws_connection = None

    def check_origin(self, origin: str) -> bool:
        """Override to enable support for allowing alternate origins.

        The ``origin`` argument is the value of the ``Origin`` HTTP
        header, the url responsible for initiating this request.  This
        method is not called for clients that do not send this header;
        such requests are always allowed (because all browsers that
        implement WebSockets support this header, and non-browser
        clients do not have the same cross-site security concerns).

        Should return ``True`` to accept the request or ``False`` to
        reject it. By default, rejects all requests with an origin on
        a host other than this one.

        This is a security protection against cross site scripting attacks on
        browsers, since WebSockets are allowed to bypass the usual same-origin
        policies and don't use CORS headers.

        .. warning::

           This is an important security measure; don't disable it
           without understanding the security implications. In
           particular, if your authentication is cookie-based, you
           must either restrict the origins allowed by
           ``check_origin()`` or implement your own XSRF-like
           protection for websocket connections. See `these
           <https://www.christian-schneider.net/CrossSiteWebSocketHijacking.html>`_
           `articles
           <https://devcenter.heroku.com/articles/websocket-security>`_
           for more.

        To accept all cross-origin traffic (which was the default prior to
        Tornado 4.0), simply override this method to always return ``True``::

            def check_origin(self, origin):
                return True

        To allow connections from any subdomain of your site, you might
        do something like::

            def check_origin(self, origin):
                parsed_origin = urllib.parse.urlparse(origin)
                return parsed_origin.netloc.endswith(".mydomain.com")

        .. versionadded:: 4.0

        """
        parsed_origin = urllib.parse.urlparse(origin)
        origin = parsed_origin.netloc
        parsed_host = urllib.parse.urlparse(self.request.full_url())
        host = parsed_host.netloc
        
        return origin == host

    def set_nodelay(self, value: bool) -> None:
        """Set the no-delay flag for this stream.

        By default, small messages may be delayed and/or combined to minimize
        the number of packets sent.  This can sometimes cause 200-500ms delays
        due to the interaction between Nagle's algorithm and TCP delayed
        ACKs.  To reduce this delay (at the expense of possibly increasing
        bandwidth usage), call ``self.set_nodelay(True)`` once the websocket
        connection is established.

        See `.BaseIOStream.set_nodelay` for additional details.

        .. versionadded:: 3.1
        """
        if self.ws_connection is not None:
            self.ws_connection.set_nodelay(value)

class WebSocketProtocol(abc.ABC):
    """Base class for WebSocket protocol versions."""

    def __init__(self, handler: '_WebSocketDelegate') -> None:
        self.handler = handler
        self.stream = None
        self.client_terminated = False
        self.server_terminated = False

    def _run_callback(self, callback: Callable, *args: Any, **kwargs: Any) -> 'Optional[Future[Any]]':
        """Runs the given callback with exception handling.

        If the callback is a coroutine, returns its Future. On error, aborts the
        websocket connection and returns None.
        """
        pass

    def _abort(self) -> None:
        """Instantly aborts the WebSocket connection by closing the socket"""
        pass

class _PerMessageDeflateCompressor(object):

    def __init__(self, persistent: bool, max_wbits: Optional[int], compression_options: Optional[Dict[str, Any]]=None) -> None:
        if max_wbits is None:
            max_wbits = zlib.MAX_WBITS
        if not 8 <= max_wbits <= zlib.MAX_WBITS:
            raise ValueError('Invalid max_wbits value %r; allowed range 8-%d', max_wbits, zlib.MAX_WBITS)
        self._max_wbits = max_wbits
        if compression_options is None or 'compression_level' not in compression_options:
            self._compression_level = tornado.web.GZipContentEncoding.GZIP_LEVEL
        else:
            self._compression_level = compression_options['compression_level']
        if compression_options is None or 'mem_level' not in compression_options:
            self._mem_level = 8
        else:
            self._mem_level = compression_options['mem_level']
        if persistent:
            self._compressor = self._create_compressor()
        else:
            self._compressor = None

class _PerMessageDeflateDecompressor(object):

    def __init__(self, persistent: bool, max_wbits: Optional[int], max_message_size: int, compression_options: Optional[Dict[str, Any]]=None) -> None:
        self._max_message_size = max_message_size
        if max_wbits is None:
            max_wbits = zlib.MAX_WBITS
        if not 8 <= max_wbits <= zlib.MAX_WBITS:
            raise ValueError('Invalid max_wbits value %r; allowed range 8-%d', max_wbits, zlib.MAX_WBITS)
        self._max_wbits = max_wbits
        if persistent:
            self._decompressor = self._create_decompressor()
        else:
            self._decompressor = None

class WebSocketProtocol13(WebSocketProtocol):
    """Implementation of the WebSocket protocol from RFC 6455.

    This class supports versions 7 and 8 of the protocol in addition to the
    final version 13.
    """
    FIN = 128
    RSV1 = 64
    RSV2 = 32
    RSV3 = 16
    RSV_MASK = RSV1 | RSV2 | RSV3
    OPCODE_MASK = 15
    stream = None

    def __init__(self, handler: '_WebSocketDelegate', mask_outgoing: bool, params: _WebSocketParams) -> None:
        WebSocketProtocol.__init__(self, handler)
        self.mask_outgoing = mask_outgoing
        self.params = params
        self._final_frame = False
        self._frame_opcode = None
        self._masked_frame = None
        self._frame_mask = None
        self._frame_length = None
        self._fragmented_message_buffer = None
        self._fragmented_message_opcode = None
        self._waiting = None
        self._compression_options = params.compression_options
        self._decompressor = None
        self._compressor = None
        self._frame_compressed = None
        self._message_bytes_in = 0
        self._message_bytes_out = 0
        self._wire_bytes_in = 0
        self._wire_bytes_out = 0
        self.ping_callback = None
        self.last_ping = 0.0
        self.last_pong = 0.0
        self.close_code = None
        self.close_reason = None

    def _handle_websocket_headers(self, handler: WebSocketHandler) -> None:
        """Verifies all invariant- and required headers

        If a header is missing or have an incorrect value ValueError will be
        raised
        """
        pass

    @staticmethod
    def compute_accept_value(key: Union[str, bytes]) -> str:
        """Computes the value for the Sec-WebSocket-Accept header,
        given the value for Sec-WebSocket-Key.
        """
        pass

    def _process_server_headers(self, key: Union[str, bytes], headers: httputil.HTTPHeaders) -> None:
        """Process the headers sent by the server to this client connection.

        'key' is the websocket handshake challenge/response key.
        """
        pass

    def _get_compressor_options(self, side: str, agreed_parameters: Dict[str, Any], compression_options: Optional[Dict[str, Any]]=None) -> Dict[str, Any]:
        """Converts a websocket agreed_parameters set to keyword arguments
        for our compressor objects.
        """
        pass

    def write_message(self, message: Union[str, bytes, Dict[str, Any]], binary: bool=False) -> 'Future[None]':
        """Sends the given message to the client of this Web Socket."""
        pass

    def write_ping(self, data: bytes) -> None:
        """Send ping frame."""
        pass

    def _handle_message(self, opcode: int, data: bytes) -> 'Optional[Future[None]]':
        """Execute on_message, returning its Future if it is a coroutine."""
        pass

    def close(self, code: Optional[int]=None, reason: Optional[str]=None) -> None:
        """Closes the WebSocket connection."""
        pass

    def is_closing(self) -> bool:
        """Return ``True`` if this connection is closing.

        The connection is considered closing if either side has
        initiated its closing handshake or if the stream has been
        shut down uncleanly.
        """
        pass

    def start_pinging(self) -> None:
        """Start sending periodic pings to keep the connection alive"""
        pass

    def periodic_ping(self) -> None:
        """Send a ping to keep the websocket alive

        Called periodically if the websocket_ping_interval is set and non-zero.
        """
        pass

class WebSocketClientConnection(simple_httpclient._HTTPConnection):
    """WebSocket client connection.

    This class should not be instantiated directly; use the
    `websocket_connect` function instead.
    """
    protocol = None

    def __init__(self, request: httpclient.HTTPRequest, on_message_callback: Optional[Callable[[Union[None, str, bytes]], None]]=None, compression_options: Optional[Dict[str, Any]]=None, ping_interval: Optional[float]=None, ping_timeout: Optional[float]=None, max_message_size: int=_default_max_message_size, subprotocols: Optional[List[str]]=None, resolver: Optional[Resolver]=None) -> None:
        self.connect_future = Future()
        self.read_queue = Queue(1)
        self.key = base64.b64encode(os.urandom(16))
        self._on_message_callback = on_message_callback
        self.close_code = None
        self.close_reason = None
        self.params = _WebSocketParams(ping_interval=ping_interval, ping_timeout=ping_timeout, max_message_size=max_message_size, compression_options=compression_options)
        scheme, sep, rest = request.url.partition(':')
        scheme = {'ws': 'http', 'wss': 'https'}[scheme]
        request.url = scheme + sep + rest
        request.headers.update({'Upgrade': 'websocket', 'Connection': 'Upgrade', 'Sec-WebSocket-Key': self.key, 'Sec-WebSocket-Version': '13'})
        if subprotocols is not None:
            request.headers['Sec-WebSocket-Protocol'] = ','.join(subprotocols)
        if compression_options is not None:
            request.headers['Sec-WebSocket-Extensions'] = 'permessage-deflate; client_max_window_bits'
        request.follow_redirects = False
        self.tcp_client = TCPClient(resolver=resolver)
        super().__init__(None, request, lambda: None, self._on_http_response, 104857600, self.tcp_client, 65536, 104857600)

    def __del__(self) -> None:
        if self.protocol is not None:
            warnings.warn('Unclosed WebSocketClientConnection', ResourceWarning)

    def close(self, code: Optional[int]=None, reason: Optional[str]=None) -> None:
        """Closes the websocket connection.

        ``code`` and ``reason`` are documented under
        `WebSocketHandler.close`.

        .. versionadded:: 3.2

        .. versionchanged:: 4.0

           Added the ``code`` and ``reason`` arguments.
        """
        pass

    def write_message(self, message: Union[str, bytes, Dict[str, Any]], binary: bool=False) -> 'Future[None]':
        """Sends a message to the WebSocket server.

        If the stream is closed, raises `WebSocketClosedError`.
        Returns a `.Future` which can be used for flow control.

        .. versionchanged:: 5.0
           Exception raised on a closed stream changed from `.StreamClosedError`
           to `WebSocketClosedError`.
        """
        pass

    def read_message(self, callback: Optional[Callable[['Future[Union[None, str, bytes]]'], None]]=None) -> Awaitable[Union[None, str, bytes]]:
        """Reads a message from the WebSocket server.

        If on_message_callback was specified at WebSocket
        initialization, this function will never return messages

        Returns a future whose result is the message, or None
        if the connection is closed.  If a callback argument
        is given it will be called with the future when it is
        ready.
        """
        pass

    def ping(self, data: bytes=b'') -> None:
        """Send ping frame to the remote end.

        The data argument allows a small amount of data (up to 125
        bytes) to be sent as a part of the ping message. Note that not
        all websocket implementations expose this data to
        applications.

        Consider using the ``ping_interval`` argument to
        `websocket_connect` instead of sending pings manually.

        .. versionadded:: 5.1

        """
        pass

    @property
    def selected_subprotocol(self) -> Optional[str]:
        """The subprotocol selected by the server.

        .. versionadded:: 5.1
        """
        pass

def websocket_connect(url: Union[str, httpclient.HTTPRequest], callback: Optional[Callable[['Future[WebSocketClientConnection]'], None]]=None, connect_timeout: Optional[float]=None, on_message_callback: Optional[Callable[[Union[None, str, bytes]], None]]=None, compression_options: Optional[Dict[str, Any]]=None, ping_interval: Optional[float]=None, ping_timeout: Optional[float]=None, max_message_size: int=_default_max_message_size, subprotocols: Optional[List[str]]=None, resolver: Optional[Resolver]=None) -> 'Awaitable[WebSocketClientConnection]':
    """Client-side websocket support.

    Takes a url and returns a Future whose result is a
    `WebSocketClientConnection`.

    ``compression_options`` is interpreted in the same way as the
    return value of `.WebSocketHandler.get_compression_options`.

    The connection supports two styles of operation. In the coroutine
    style, the application typically calls
    `~.WebSocketClientConnection.read_message` in a loop::

        conn = yield websocket_connect(url)
        while True:
            msg = yield conn.read_message()
            if msg is None: break
            # Do something with msg

    In the callback style, pass an ``on_message_callback`` to
    ``websocket_connect``. In both styles, a message of ``None``
    indicates that the connection has been closed.

    ``subprotocols`` may be a list of strings specifying proposed
    subprotocols. The selected protocol may be found on the
    ``selected_subprotocol`` attribute of the connection object
    when the connection is complete.

    .. versionchanged:: 3.2
       Also accepts ``HTTPRequest`` objects in place of urls.

    .. versionchanged:: 4.1
       Added ``compression_options`` and ``on_message_callback``.

    .. versionchanged:: 4.5
       Added the ``ping_interval``, ``ping_timeout``, and ``max_message_size``
       arguments, which have the same meaning as in `WebSocketHandler`.

    .. versionchanged:: 5.0
       The ``io_loop`` argument (deprecated since version 4.1) has been removed.

    .. versionchanged:: 5.1
       Added the ``subprotocols`` argument.

    .. versionchanged:: 6.3
       Added the ``resolver`` argument.
    """
    pass
