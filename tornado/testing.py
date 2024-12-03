"""Support classes for automated testing.

* `AsyncTestCase` and `AsyncHTTPTestCase`:  Subclasses of unittest.TestCase
  with additional support for testing asynchronous (`.IOLoop`-based) code.

* `ExpectLog`: Make test logs less spammy.

* `main()`: A simple test runner (wrapper around unittest.main()) with support
  for the tornado.autoreload module to rerun the tests when code changes.
"""
import asyncio
from collections.abc import Generator
import functools
import inspect
import logging
import os
import re
import signal
import socket
import sys
import unittest
import warnings
from tornado import gen
from tornado.httpclient import AsyncHTTPClient, HTTPResponse
from tornado.httpserver import HTTPServer
from tornado.ioloop import IOLoop, TimeoutError
from tornado import netutil
from tornado.platform.asyncio import AsyncIOMainLoop
from tornado.process import Subprocess
from tornado.log import app_log
from tornado.util import raise_exc_info, basestring_type
from tornado.web import Application
import typing
from typing import Tuple, Any, Callable, Type, Dict, Union, Optional, Coroutine
from types import TracebackType
if typing.TYPE_CHECKING:
    _ExcInfoTuple = Tuple[Optional[Type[BaseException]], Optional[BaseException], Optional[TracebackType]]
_NON_OWNED_IOLOOPS = AsyncIOMainLoop

def bind_unused_port(reuse_port: bool=False, address: str='127.0.0.1') -> Tuple[socket.socket, int]:
    """Binds a server socket to an available port on localhost.

    Returns a tuple (socket, port).

    .. versionchanged:: 4.4
       Always binds to ``127.0.0.1`` without resolving the name
       ``localhost``.

    .. versionchanged:: 6.2
       Added optional ``address`` argument to
       override the default "127.0.0.1".
    """
    sock = socket.socket()
    if reuse_port:
        if not hasattr(socket, "SO_REUSEPORT"):
            raise ValueError("reuse_port not supported on this platform")
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    
    # Bind to a free port. By using 0 as the port number, 
    # the system will allocate an unused port for us.
    sock.bind((address, 0))
    
    # Get the actual port number that was allocated
    port = sock.getsockname()[1]
    return sock, port

def get_async_test_timeout() -> float:
    """Get the global timeout setting for async tests.

    Returns a float, the timeout in seconds.

    .. versionadded:: 3.1
    """
    try:
        return float(os.environ.get('ASYNC_TEST_TIMEOUT', 5))
    except ValueError:
        return 5.0

class AsyncTestCase(unittest.TestCase):
    """`~unittest.TestCase` subclass for testing `.IOLoop`-based
    asynchronous code.

    The unittest framework is synchronous, so the test must be
    complete by the time the test method returns. This means that
    asynchronous code cannot be used in quite the same way as usual
    and must be adapted to fit. To write your tests with coroutines,
    decorate your test methods with `tornado.testing.gen_test` instead
    of `tornado.gen.coroutine`.

    This class also provides the (deprecated) `stop()` and `wait()`
    methods for a more manual style of testing. The test method itself
    must call ``self.wait()``, and asynchronous callbacks should call
    ``self.stop()`` to signal completion.

    By default, a new `.IOLoop` is constructed for each test and is available
    as ``self.io_loop``.  If the code being tested requires a
    reused global `.IOLoop`, subclasses should override `get_new_ioloop` to return it,
    although this is deprecated as of Tornado 6.3.

    The `.IOLoop`'s ``start`` and ``stop`` methods should not be
    called directly.  Instead, use `self.stop <stop>` and `self.wait
    <wait>`.  Arguments passed to ``self.stop`` are returned from
    ``self.wait``.  It is possible to have multiple ``wait``/``stop``
    cycles in the same test.

    Example::

        # This test uses coroutine style.
        class MyTestCase(AsyncTestCase):
            @tornado.testing.gen_test
            def test_http_fetch(self):
                client = AsyncHTTPClient()
                response = yield client.fetch("http://www.tornadoweb.org")
                # Test contents of response
                self.assertIn("FriendFeed", response.body)

        # This test uses argument passing between self.stop and self.wait.
        class MyTestCase2(AsyncTestCase):
            def test_http_fetch(self):
                client = AsyncHTTPClient()
                client.fetch("http://www.tornadoweb.org/", self.stop)
                response = self.wait()
                # Test contents of response
                self.assertIn("FriendFeed", response.body)
    """

    def __init__(self, methodName: str='runTest') -> None:
        super().__init__(methodName)
        self.__stopped = False
        self.__running = False
        self.__failure = None
        self.__stop_args = None
        self.__timeout = None
        self._test_generator = None
        self.io_loop = None

    def get_new_ioloop(self) -> IOLoop:
        """Returns the `.IOLoop` to use for this test.

        By default, a new `.IOLoop` is created for each test.
        Subclasses may override this method to return
        `.IOLoop.current()` if it is not appropriate to use a new
        `.IOLoop` in each tests (for example, if there are global
        singletons using the default `.IOLoop`) or if a per-test event
        loop is being provided by another system (such as
        ``pytest-asyncio``).

        .. deprecated:: 6.3
           This method will be removed in Tornado 7.0.
        """
        warnings.warn("get_new_ioloop is deprecated, use asyncio directly", DeprecationWarning)
        return IOLoop()

    def _callTestMethod(self, method: Callable) -> None:
        """Run the given test method, raising an error if it returns non-None.

        Failure to decorate asynchronous test methods with ``@gen_test`` can lead to tests
        incorrectly passing.

        Remove this override when Python 3.10 support is dropped. This check (in the form of a
        DeprecationWarning) became a part of the standard library in 3.11.

        Note that ``_callTestMethod`` is not documented as a public interface. However, it is
        present in all supported versions of Python (3.8+), and if it goes away in the future that's
        OK because we can just remove this override as noted above.
        """
        result = method()
        if result is not None:
            warnings.warn(f"Test method {method.__name__} should return None, not {result!r}",
                          DeprecationWarning, stacklevel=2)
        super()._callTestMethod(method)

    def stop(self, _arg: Any=None, **kwargs: Any) -> None:
        """Stops the `.IOLoop`, causing one pending (or future) call to `wait()`
        to return.

        Keyword arguments or a single positional argument passed to `stop()` are
        saved and will be returned by `wait()`.

        .. deprecated:: 5.1

           `stop` and `wait` are deprecated; use ``@gen_test`` instead.
        """
        warnings.warn("stop is deprecated, use @gen_test instead", DeprecationWarning)
        self.__stop_args = kwargs or _arg
        if self.__running:
            self.io_loop.stop()
            self.__running = False
        self.__stopped = True

    def wait(self, condition: Optional[Callable[..., bool]]=None, timeout: Optional[float]=None) -> Any:
        """Runs the `.IOLoop` until stop is called or timeout has passed.

        In the event of a timeout, an exception will be thrown. The
        default timeout is 5 seconds; it may be overridden with a
        ``timeout`` keyword argument or globally with the
        ``ASYNC_TEST_TIMEOUT`` environment variable.

        If ``condition`` is not ``None``, the `.IOLoop` will be restarted
        after `stop()` until ``condition()`` returns ``True``.

        .. versionchanged:: 3.1
           Added the ``ASYNC_TEST_TIMEOUT`` environment variable.

        .. deprecated:: 5.1

           `stop` and `wait` are deprecated; use ``@gen_test`` instead.
        """
        warnings.warn("wait is deprecated, use @gen_test instead", DeprecationWarning)
        if timeout is None:
            timeout = get_async_test_timeout()

        if not self.__stopped:
            if timeout:
                def timeout_func():
                    try:
                        raise TimeoutError('Async operation timed out after %s seconds' % timeout)
                    except Exception:
                        self.__failure = sys.exc_info()
                    self.stop()
                self.io_loop.add_timeout(self.io_loop.time() + timeout, timeout_func)
            while True:
                self.__running = True
                self.io_loop.start()
                if (self.__failure is not None or
                    condition is None or condition()):
                    break
        assert self.__stopped
        self.__stopped = False
        if self.__failure is not None:
            raise_exc_info(self.__failure)
        result = self.__stop_args
        self.__stop_args = None
        return result

class AsyncHTTPTestCase(AsyncTestCase):
    """A test case that starts up an HTTP server.

    Subclasses must override `get_app()`, which returns the
    `tornado.web.Application` (or other `.HTTPServer` callback) to be tested.
    Tests will typically use the provided ``self.http_client`` to fetch
    URLs from this server.

    Example, assuming the "Hello, world" example from the user guide is in
    ``hello.py``::

        import hello

        class TestHelloApp(AsyncHTTPTestCase):
            def get_app(self):
                return hello.make_app()

            def test_homepage(self):
                response = self.fetch('/')
                self.assertEqual(response.code, 200)
                self.assertEqual(response.body, 'Hello, world')

    That call to ``self.fetch()`` is equivalent to ::

        self.http_client.fetch(self.get_url('/'), self.stop)
        response = self.wait()

    which illustrates how AsyncTestCase can turn an asynchronous operation,
    like ``http_client.fetch()``, into a synchronous operation. If you need
    to do other asynchronous operations in tests, you'll probably need to use
    ``stop()`` and ``wait()`` yourself.
    """

    def get_app(self) -> Application:
        """Should be overridden by subclasses to return a
        `tornado.web.Application` or other `.HTTPServer` callback.
        """
        raise NotImplementedError()

    def fetch(self, path: str, raise_error: bool=False, **kwargs: Any) -> HTTPResponse:
        """Convenience method to synchronously fetch a URL.

        The given path will be appended to the local server's host and
        port.  Any additional keyword arguments will be passed directly to
        `.AsyncHTTPClient.fetch` (and so could be used to pass
        ``method="POST"``, ``body="..."``, etc).

        If the path begins with http:// or https://, it will be treated as a
        full URL and will be fetched as-is.

        If ``raise_error`` is ``True``, a `tornado.httpclient.HTTPError` will
        be raised if the response code is not 200. This is the same behavior
        as the ``raise_error`` argument to `.AsyncHTTPClient.fetch`, but
        the default is ``False`` here (it's ``True`` in `.AsyncHTTPClient`)
        because tests often need to deal with non-200 response codes.

        .. versionchanged:: 5.0
           Added support for absolute URLs.

        .. versionchanged:: 5.1

           Added the ``raise_error`` argument.

        .. deprecated:: 5.1

           This method currently turns any exception into an
           `.HTTPResponse` with status code 599. In Tornado 6.0,
           errors other than `tornado.httpclient.HTTPError` will be
           passed through, and ``raise_error=False`` will only
           suppress errors that would be raised due to non-200
           response codes.

        """
        if path.startswith(('http://', 'https://')):
            url = path
        else:
            url = self.get_url(path)
        
        self.http_client.fetch(url, self.stop, raise_error=raise_error, **kwargs)
        return self.wait()

    def get_httpserver_options(self) -> Dict[str, Any]:
        """May be overridden by subclasses to return additional
        keyword arguments for the server.
        """
        return {}

    def get_http_port(self) -> int:
        """Returns the port used by the server.

        A new port is chosen for each test.
        """
        return self.http_server.port

    def get_url(self, path: str) -> str:
        """Returns an absolute url for the given path on the test server."""
        return f"http://localhost:{self.get_http_port()}{path}"

class AsyncHTTPSTestCase(AsyncHTTPTestCase):
    """A test case that starts an HTTPS server.

    Interface is generally the same as `AsyncHTTPTestCase`.
    """

    def get_ssl_options(self) -> Dict[str, Any]:
        """May be overridden by subclasses to select SSL options.

        By default includes a self-signed testing certificate.
        """
        # Testing keys were generated with:
        # openssl req -new -keyout localhost.key -out localhost.csr -subj '/CN=localhost'
        # openssl x509 -req -days 3650 -in localhost.csr -signkey localhost.key -out localhost.crt
        module_dir = os.path.dirname(__file__)
        return dict(
            certfile=os.path.join(module_dir, 'test', 'localhost.crt'),
            keyfile=os.path.join(module_dir, 'test', 'localhost.key'),
        )

def gen_test(func: Optional[Callable[..., Union[Generator, 'Coroutine']]]=None, timeout: Optional[float]=None) -> Union[Callable[..., None], Callable[[Callable[..., Union[Generator, 'Coroutine']]], Callable[..., None]]]:
    """Testing equivalent of ``@gen.coroutine``, to be applied to test methods.

    ``@gen.coroutine`` cannot be used on tests because the `.IOLoop` is not
    already running.  ``@gen_test`` should be applied to test methods
    on subclasses of `AsyncTestCase`.

    Example::

        class MyTest(AsyncHTTPTestCase):
            @gen_test
            def test_something(self):
                response = yield self.http_client.fetch(self.get_url('/'))

    By default, ``@gen_test`` times out after 5 seconds. The timeout may be
    overridden globally with the ``ASYNC_TEST_TIMEOUT`` environment variable,
    or for each test with the ``timeout`` keyword argument::

        class MyTest(AsyncHTTPTestCase):
            @gen_test(timeout=10)
            def test_something_slow(self):
                response = yield self.http_client.fetch(self.get_url('/'))

    Note that ``@gen_test`` is incompatible with `AsyncTestCase.stop`,
    `AsyncTestCase.wait`, and `AsyncHTTPTestCase.fetch`. Use ``yield
    self.http_client.fetch(self.get_url())`` as shown above instead.

    .. versionadded:: 3.1
       The ``timeout`` argument and ``ASYNC_TEST_TIMEOUT`` environment
       variable.

    .. versionchanged:: 4.0
       The wrapper now passes along ``*args, **kwargs`` so it can be used
       on functions with arguments.

    """
    if timeout is None:
        timeout = get_async_test_timeout()

    def wrapper(f):
        @functools.wraps(f)
        def wrapped(self, *args, **kwargs):
            future = gen.convert_yielded(f(self, *args, **kwargs))
            if timeout:
                future = gen.with_timeout(datetime.timedelta(seconds=timeout), future)
            self.io_loop.run_sync(lambda: future)
        return wrapped

    if func is not None:
        return wrapper(func)
    return wrapper

gen_test.__test__ = False

class ExpectLog(logging.Filter):
    """Context manager to capture and suppress expected log output.

    Useful to make tests of error conditions less noisy, while still
    leaving unexpected log entries visible.  *Not thread safe.*

    The attribute ``logged_stack`` is set to ``True`` if any exception
    stack trace was logged.

    Usage::

        with ExpectLog('tornado.application', "Uncaught exception"):
            error_response = self.fetch("/some_page")

    .. versionchanged:: 4.3
       Added the ``logged_stack`` attribute.
    """

    def __init__(self, logger: Union[logging.Logger, basestring_type], regex: str, required: bool=True, level: Optional[int]=None) -> None:
        """Constructs an ExpectLog context manager.

        :param logger: Logger object (or name of logger) to watch.  Pass an
            empty string to watch the root logger.
        :param regex: Regular expression to match.  Any log entries on the
            specified logger that match this regex will be suppressed.
        :param required: If true, an exception will be raised if the end of the
            ``with`` statement is reached without matching any log entries.
        :param level: A constant from the ``logging`` module indicating the
            expected log level. If this parameter is provided, only log messages
            at this level will be considered to match. Additionally, the
            supplied ``logger`` will have its level adjusted if necessary (for
            the duration of the ``ExpectLog`` to enable the expected message.

        .. versionchanged:: 6.1
           Added the ``level`` parameter.

        .. deprecated:: 6.3
           In Tornado 7.0, only ``WARNING`` and higher logging levels will be
           matched by default. To match ``INFO`` and lower levels, the ``level``
           argument must be used. This is changing to minimize differences
           between ``tornado.testing.main`` (which enables ``INFO`` logs by
           default) and most other test runners (including those in IDEs)
           which have ``INFO`` logs disabled by default.
        """
        if isinstance(logger, basestring_type):
            logger = logging.getLogger(logger)
        self.logger = logger
        self.regex = re.compile(regex)
        self.required = required
        self.matched = 0
        self.deprecated_level_matched = 0
        self.logged_stack = False
        self.level = level
        self.orig_level = None

    def __enter__(self) -> 'ExpectLog':
        if self.level is not None and self.level < self.logger.getEffectiveLevel():
            self.orig_level = self.logger.level
            self.logger.setLevel(self.level)
        self.logger.addFilter(self)
        return self

    def __exit__(self, typ: 'Optional[Type[BaseException]]', value: Optional[BaseException], tb: Optional[TracebackType]) -> None:
        if self.orig_level is not None:
            self.logger.setLevel(self.orig_level)
        self.logger.removeFilter(self)
        if not typ and self.required and (not self.matched):
            raise Exception('did not get expected log message')
        if not typ and self.required and (self.deprecated_level_matched >= self.matched):
            warnings.warn('ExpectLog matched at INFO or below without level argument', DeprecationWarning)

def setup_with_context_manager(testcase: unittest.TestCase, cm: Any) -> Any:
    """Use a contextmanager to setUp a test case."""
    pass

def main(**kwargs: Any) -> None:
    """A simple test runner.

    This test runner is essentially equivalent to `unittest.main` from
    the standard library, but adds support for Tornado-style option
    parsing and log formatting. It is *not* necessary to use this
    `main` function to run tests using `AsyncTestCase`; these tests
    are self-contained and can run with any test runner.

    The easiest way to run a test is via the command line::

        python -m tornado.testing tornado.test.web_test

    See the standard library ``unittest`` module for ways in which
    tests can be specified.

    Projects with many tests may wish to define a test script like
    ``tornado/test/runtests.py``.  This script should define a method
    ``all()`` which returns a test suite and then call
    `tornado.testing.main()`.  Note that even when a test script is
    used, the ``all()`` test suite may be overridden by naming a
    single test on the command line::

        # Runs all tests
        python -m tornado.test.runtests
        # Runs one test
        python -m tornado.test.runtests tornado.test.web_test

    Additional keyword arguments passed through to ``unittest.main()``.
    For example, use ``tornado.testing.main(verbosity=2)``
    to show many test details as they are run.
    See http://docs.python.org/library/unittest.html#unittest.main
    for full argument list.

    .. versionchanged:: 5.0

       This function produces no output of its own; only that produced
       by the `unittest` module (previously it would add a PASS or FAIL
       log message).
    """
    pass
if __name__ == '__main__':
    main()
