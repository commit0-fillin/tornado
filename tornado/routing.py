"""Flexible routing implementation.

Tornado routes HTTP requests to appropriate handlers using `Router`
class implementations. The `tornado.web.Application` class is a
`Router` implementation and may be used directly, or the classes in
this module may be used for additional flexibility. The `RuleRouter`
class can match on more criteria than `.Application`, or the `Router`
interface can be subclassed for maximum customization.

`Router` interface extends `~.httputil.HTTPServerConnectionDelegate`
to provide additional routing capabilities. This also means that any
`Router` implementation can be used directly as a ``request_callback``
for `~.httpserver.HTTPServer` constructor.

`Router` subclass must implement a ``find_handler`` method to provide
a suitable `~.httputil.HTTPMessageDelegate` instance to handle the
request:

.. code-block:: python

    class CustomRouter(Router):
        def find_handler(self, request, **kwargs):
            # some routing logic providing a suitable HTTPMessageDelegate instance
            return MessageDelegate(request.connection)

    class MessageDelegate(HTTPMessageDelegate):
        def __init__(self, connection):
            self.connection = connection

        def finish(self):
            self.connection.write_headers(
                ResponseStartLine("HTTP/1.1", 200, "OK"),
                HTTPHeaders({"Content-Length": "2"}),
                b"OK")
            self.connection.finish()

    router = CustomRouter()
    server = HTTPServer(router)

The main responsibility of `Router` implementation is to provide a
mapping from a request to `~.httputil.HTTPMessageDelegate` instance
that will handle this request. In the example above we can see that
routing is possible even without instantiating an `~.web.Application`.

For routing to `~.web.RequestHandler` implementations we need an
`~.web.Application` instance. `~.web.Application.get_handler_delegate`
provides a convenient way to create `~.httputil.HTTPMessageDelegate`
for a given request and `~.web.RequestHandler`.

Here is a simple example of how we can we route to
`~.web.RequestHandler` subclasses by HTTP method:

.. code-block:: python

    resources = {}

    class GetResource(RequestHandler):
        def get(self, path):
            if path not in resources:
                raise HTTPError(404)

            self.finish(resources[path])

    class PostResource(RequestHandler):
        def post(self, path):
            resources[path] = self.request.body

    class HTTPMethodRouter(Router):
        def __init__(self, app):
            self.app = app

        def find_handler(self, request, **kwargs):
            handler = GetResource if request.method == "GET" else PostResource
            return self.app.get_handler_delegate(request, handler, path_args=[request.path])

    router = HTTPMethodRouter(Application())
    server = HTTPServer(router)

`ReversibleRouter` interface adds the ability to distinguish between
the routes and reverse them to the original urls using route's name
and additional arguments. `~.web.Application` is itself an
implementation of `ReversibleRouter` class.

`RuleRouter` and `ReversibleRuleRouter` are implementations of
`Router` and `ReversibleRouter` interfaces and can be used for
creating rule-based routing configurations.

Rules are instances of `Rule` class. They contain a `Matcher`, which
provides the logic for determining whether the rule is a match for a
particular request and a target, which can be one of the following.

1) An instance of `~.httputil.HTTPServerConnectionDelegate`:

.. code-block:: python

    router = RuleRouter([
        Rule(PathMatches("/handler"), ConnectionDelegate()),
        # ... more rules
    ])

    class ConnectionDelegate(HTTPServerConnectionDelegate):
        def start_request(self, server_conn, request_conn):
            return MessageDelegate(request_conn)

2) A callable accepting a single argument of `~.httputil.HTTPServerRequest` type:

.. code-block:: python

    router = RuleRouter([
        Rule(PathMatches("/callable"), request_callable)
    ])

    def request_callable(request):
        request.write(b"HTTP/1.1 200 OK\\r\\nContent-Length: 2\\r\\n\\r\\nOK")
        request.finish()

3) Another `Router` instance:

.. code-block:: python

    router = RuleRouter([
        Rule(PathMatches("/router.*"), CustomRouter())
    ])

Of course a nested `RuleRouter` or a `~.web.Application` is allowed:

.. code-block:: python

    router = RuleRouter([
        Rule(HostMatches("example.com"), RuleRouter([
            Rule(PathMatches("/app1/.*"), Application([(r"/app1/handler", Handler)])),
        ]))
    ])

    server = HTTPServer(router)

In the example below `RuleRouter` is used to route between applications:

.. code-block:: python

    app1 = Application([
        (r"/app1/handler", Handler1),
        # other handlers ...
    ])

    app2 = Application([
        (r"/app2/handler", Handler2),
        # other handlers ...
    ])

    router = RuleRouter([
        Rule(PathMatches("/app1.*"), app1),
        Rule(PathMatches("/app2.*"), app2)
    ])

    server = HTTPServer(router)

For more information on application-level routing see docs for `~.web.Application`.

.. versionadded:: 4.5

"""
import re
from functools import partial
from tornado import httputil
from tornado.httpserver import _CallableAdapter
from tornado.escape import url_escape, url_unescape, utf8
from tornado.log import app_log
from tornado.util import basestring_type, import_object, re_unescape, unicode_type
from typing import Any, Union, Optional, Awaitable, List, Dict, Pattern, Tuple, overload

class Router(httputil.HTTPServerConnectionDelegate):
    """Abstract router interface."""

    def find_handler(self, request: httputil.HTTPServerRequest, **kwargs: Any) -> Optional[httputil.HTTPMessageDelegate]:
        """Must be implemented to return an appropriate instance of `~.httputil.HTTPMessageDelegate`
        that can serve the request.
        Routing implementations may pass additional kwargs to extend the routing logic.

        :arg httputil.HTTPServerRequest request: current HTTP request.
        :arg kwargs: additional keyword arguments passed by routing implementation.
        :returns: an instance of `~.httputil.HTTPMessageDelegate` that will be used to
            process the request.
        """
        raise NotImplementedError("Router subclasses must implement find_handler()")

class ReversibleRouter(Router):
    """Abstract router interface for routers that can handle named routes
    and support reversing them to original urls.
    """

    def reverse_url(self, name: str, *args: Any) -> Optional[str]:
        """Returns url string for a given route name and arguments
        or ``None`` if no match is found.

        :arg str name: route name.
        :arg args: url parameters.
        :returns: parametrized url string for a given route name (or ``None``).
        """
        raise NotImplementedError("Router subclasses must implement reverse_url()")

class _RoutingDelegate(httputil.HTTPMessageDelegate):

    def __init__(self, router: Router, server_conn: object, request_conn: httputil.HTTPConnection) -> None:
        self.server_conn = server_conn
        self.request_conn = request_conn
        self.delegate = None
        self.router = router

class _DefaultMessageDelegate(httputil.HTTPMessageDelegate):

    def __init__(self, connection: httputil.HTTPConnection) -> None:
        self.connection = connection
_RuleList = List[Union['Rule', List[Any], Tuple[Union[str, 'Matcher'], Any], Tuple[Union[str, 'Matcher'], Any, Dict[str, Any]], Tuple[Union[str, 'Matcher'], Any, Dict[str, Any], str]]]

class RuleRouter(Router):
    """Rule-based router implementation."""

    def __init__(self, rules: Optional[_RuleList]=None) -> None:
        """Constructs a router from an ordered list of rules::

            RuleRouter([
                Rule(PathMatches("/handler"), Target),
                # ... more rules
            ])

        You can also omit explicit `Rule` constructor and use tuples of arguments::

            RuleRouter([
                (PathMatches("/handler"), Target),
            ])

        `PathMatches` is a default matcher, so the example above can be simplified::

            RuleRouter([
                ("/handler", Target),
            ])

        In the examples above, ``Target`` can be a nested `Router` instance, an instance of
        `~.httputil.HTTPServerConnectionDelegate` or an old-style callable,
        accepting a request argument.

        :arg rules: a list of `Rule` instances or tuples of `Rule`
            constructor arguments.
        """
        self.rules = []
        if rules:
            self.add_rules(rules)

    def add_rules(self, rules: _RuleList) -> None:
        """Appends new rules to the router.

        :arg rules: a list of Rule instances (or tuples of arguments, which are
            passed to Rule constructor).
        """
        for rule in rules:
            if isinstance(rule, (tuple, list)):
                assert len(rule) in (2, 3, 4)
                if isinstance(rule[0], str):
                    rule = Rule(PathMatches(rule[0]), *rule[1:])
                else:
                    rule = Rule(*rule)
            self.rules.append(self.process_rule(rule))

    def process_rule(self, rule: 'Rule') -> 'Rule':
        """Override this method for additional preprocessing of each rule.

        :arg Rule rule: a rule to be processed.
        :returns: the same or modified Rule instance.
        """
        return rule

    def get_target_delegate(self, target: Any, request: httputil.HTTPServerRequest, **target_params: Any) -> Optional[httputil.HTTPMessageDelegate]:
        """Returns an instance of `~.httputil.HTTPMessageDelegate` for a
        Rule's target. This method is called by `~.find_handler` and can be
        extended to provide additional target types.

        :arg target: a Rule's target.
        :arg httputil.HTTPServerRequest request: current request.
        :arg target_params: additional parameters that can be useful
            for `~.httputil.HTTPMessageDelegate` creation.
        """
        if isinstance(target, httputil.HTTPServerConnectionDelegate):
            return target.start_request(request.server_connection, request.connection)
        elif callable(target):
            return _CallableAdapter(target, request.connection)
        elif isinstance(target, Router):
            return target.find_handler(request, **target_params)
        else:
            return None

class ReversibleRuleRouter(ReversibleRouter, RuleRouter):
    """A rule-based router that implements ``reverse_url`` method.

    Each rule added to this router may have a ``name`` attribute that can be
    used to reconstruct an original uri. The actual reconstruction takes place
    in a rule's matcher (see `Matcher.reverse`).
    """

    def __init__(self, rules: Optional[_RuleList]=None) -> None:
        self.named_rules = {}
        super().__init__(rules)

class Rule(object):
    """A routing rule."""

    def __init__(self, matcher: 'Matcher', target: Any, target_kwargs: Optional[Dict[str, Any]]=None, name: Optional[str]=None) -> None:
        """Constructs a Rule instance.

        :arg Matcher matcher: a `Matcher` instance used for determining
            whether the rule should be considered a match for a specific
            request.
        :arg target: a Rule's target (typically a ``RequestHandler`` or
            `~.httputil.HTTPServerConnectionDelegate` subclass or even a nested `Router`,
            depending on routing implementation).
        :arg dict target_kwargs: a dict of parameters that can be useful
            at the moment of target instantiation (for example, ``status_code``
            for a ``RequestHandler`` subclass). They end up in
            ``target_params['target_kwargs']`` of `RuleRouter.get_target_delegate`
            method.
        :arg str name: the name of the rule that can be used to find it
            in `ReversibleRouter.reverse_url` implementation.
        """
        if isinstance(target, str):
            target = import_object(target)
        self.matcher = matcher
        self.target = target
        self.target_kwargs = target_kwargs if target_kwargs else {}
        self.name = name

    def __repr__(self) -> str:
        return '%s(%r, %s, kwargs=%r, name=%r)' % (self.__class__.__name__, self.matcher, self.target, self.target_kwargs, self.name)

class Matcher(object):
    """Represents a matcher for request features."""

    def match(self, request: httputil.HTTPServerRequest) -> Optional[Dict[str, Any]]:
        """Matches current instance against the request.

        :arg httputil.HTTPServerRequest request: current HTTP request
        :returns: a dict of parameters to be passed to the target handler
            (for example, ``handler_kwargs``, ``path_args``, ``path_kwargs``
            can be passed for proper `~.web.RequestHandler` instantiation).
            An empty dict is a valid (and common) return value to indicate a match
            when the argument-passing features are not used.
            ``None`` must be returned to indicate that there is no match."""
        raise NotImplementedError("Matcher subclasses must implement match()")

    def reverse(self, *args: Any) -> Optional[str]:
        """Reconstructs full url from matcher instance and additional arguments."""
        return None

class AnyMatches(Matcher):
    """Matches any request."""

class HostMatches(Matcher):
    """Matches requests from hosts specified by ``host_pattern`` regex."""

    def __init__(self, host_pattern: Union[str, Pattern]) -> None:
        if isinstance(host_pattern, basestring_type):
            if not host_pattern.endswith('$'):
                host_pattern += '$'
            self.host_pattern = re.compile(host_pattern)
        else:
            self.host_pattern = host_pattern

class DefaultHostMatches(Matcher):
    """Matches requests from host that is equal to application's default_host.
    Always returns no match if ``X-Real-Ip`` header is present.
    """

    def __init__(self, application: Any, host_pattern: Pattern) -> None:
        self.application = application
        self.host_pattern = host_pattern

class PathMatches(Matcher):
    """Matches requests with paths specified by ``path_pattern`` regex."""

    def __init__(self, path_pattern: Union[str, Pattern]) -> None:
        if isinstance(path_pattern, basestring_type):
            if not path_pattern.endswith('$'):
                path_pattern += '$'
            self.regex = re.compile(path_pattern)
        else:
            self.regex = path_pattern
        assert len(self.regex.groupindex) in (0, self.regex.groups), 'groups in url regexes must either be all named or all positional: %r' % self.regex.pattern
        self._path, self._group_count = self._find_groups()

    def _find_groups(self) -> Tuple[Optional[str], Optional[int]]:
        """Returns a tuple (reverse string, group count) for a url.

        For example: Given the url pattern /([0-9]{4})/([a-z-]+)/, this method
        would return ('/%s/%s/', 2).
        """
        pattern = self.regex.pattern
        if pattern.startswith('^'):
            pattern = pattern[1:]
        if pattern.endswith('$'):
            pattern = pattern[:-1]

        if not pattern.startswith('/'):
            pattern = '/' + pattern

        group_count = len(self.regex.groupindex)

        if group_count == 0:
            group_count = self.regex.groups
        elif group_count < self.regex.groups:
            raise ValueError("url has unnamed groups in %r" % self.regex.pattern)

        reverse_pattern = re.sub(r'\\.', lambda m: m.group(0), pattern)
        reverse_pattern = re.sub(r'[^(]*\((?!\?)', '%s', reverse_pattern)
        reverse_pattern = re.sub(r'\([^)]*\)', '', reverse_pattern)
        return reverse_pattern, group_count

class URLSpec(Rule):
    """Specifies mappings between URLs and handlers.

    .. versionchanged: 4.5
       `URLSpec` is now a subclass of a `Rule` with `PathMatches` matcher and is preserved for
       backwards compatibility.
    """

    def __init__(self, pattern: Union[str, Pattern], handler: Any, kwargs: Optional[Dict[str, Any]]=None, name: Optional[str]=None) -> None:
        """Parameters:

        * ``pattern``: Regular expression to be matched. Any capturing
          groups in the regex will be passed in to the handler's
          get/post/etc methods as arguments (by keyword if named, by
          position if unnamed. Named and unnamed capturing groups
          may not be mixed in the same rule).

        * ``handler``: `~.web.RequestHandler` subclass to be invoked.

        * ``kwargs`` (optional): A dictionary of additional arguments
          to be passed to the handler's constructor.

        * ``name`` (optional): A name for this handler.  Used by
          `~.web.Application.reverse_url`.

        """
        matcher = PathMatches(pattern)
        super().__init__(matcher, handler, kwargs, name)
        self.regex = matcher.regex
        self.handler_class = self.target
        self.kwargs = kwargs

    def __repr__(self) -> str:
        return '%s(%r, %s, kwargs=%r, name=%r)' % (self.__class__.__name__, self.regex.pattern, self.handler_class, self.kwargs, self.name)

def _unquote_or_none(s: Optional[str]) -> Optional[bytes]:
    """None-safe wrapper around url_unescape to handle unmatched optional
    groups correctly.

    Note that args are passed as bytes so the handler can decide what
    encoding to use.
    """
    if s is None:
        return None
    return utf8(url_unescape(s))
