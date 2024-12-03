"""Non-blocking HTTP client implementation using pycurl."""
import collections
import functools
import logging
import pycurl
import re
import threading
import time
from io import BytesIO
from tornado import httputil
from tornado import ioloop
from tornado.escape import utf8, native_str
from tornado.httpclient import HTTPRequest, HTTPResponse, HTTPError, AsyncHTTPClient, main
from tornado.log import app_log
from typing import Dict, Any, Callable, Union, Optional
import typing
if typing.TYPE_CHECKING:
    from typing import Deque, Tuple
curl_log = logging.getLogger('tornado.curl_httpclient')
CR_OR_LF_RE = re.compile(b'\r|\n')

class CurlAsyncHTTPClient(AsyncHTTPClient):

    def _handle_socket(self, event: int, fd: int, multi: Any, data: bytes) -> None:
        """Called by libcurl when it wants to change the file descriptors
        it cares about.
        """
        if event == pycurl.POLL_NONE:
            self.io_loop.remove_handler(fd)
        else:
            if event == pycurl.POLL_IN:
                self.io_loop.add_handler(fd, self._handle_events, self.io_loop.READ)
            elif event == pycurl.POLL_OUT:
                self.io_loop.add_handler(fd, self._handle_events, self.io_loop.WRITE)
            elif event == pycurl.POLL_INOUT:
                self.io_loop.add_handler(fd, self._handle_events, self.io_loop.READ | self.io_loop.WRITE)

    def _set_timeout(self, msecs: int) -> None:
        """Called by libcurl to schedule a timeout."""
        if self._timeout is not None:
            self.io_loop.remove_timeout(self._timeout)
        self._timeout = self.io_loop.add_timeout(self.io_loop.time() + msecs / 1000.0, self._handle_timeout)

    def _handle_events(self, fd: int, events: int) -> None:
        """Called by IOLoop when there is activity on one of our
        file descriptors.
        """
        action = 0
        if events & self.io_loop.READ:
            action |= pycurl.CSELECT_IN
        if events & self.io_loop.WRITE:
            action |= pycurl.CSELECT_OUT
        while True:
            try:
                ret, num_handles = self._multi.socket_action(fd, action)
            except pycurl.error as e:
                ret = e.args[0]
            if ret != pycurl.E_CALL_MULTI_PERFORM:
                break
        self._finish_pending_requests()

    def _handle_timeout(self) -> None:
        """Called by IOLoop when the requested timeout has passed."""
        with self.exception_logging():
            ret, num_handles = self._multi.socket_action(pycurl.SOCKET_TIMEOUT, 0)
        self._finish_pending_requests()

    def _handle_force_timeout(self) -> None:
        """Called by IOLoop periodically to ask libcurl to process any
        events it may have forgotten about.
        """
        with self.exception_logging():
            ret, num_handles = self._multi.socket_action(pycurl.SOCKET_TIMEOUT, 0)
        self._finish_pending_requests()

    def _finish_pending_requests(self) -> None:
        """Process any requests that were completed by the last
        call to multi.socket_action.
        """
        while True:
            num_q, ok_list, err_list = self._multi.info_read()
            for curl in ok_list:
                self._finish(curl)
            for curl, errnum, errmsg in err_list:
                self._finish(curl, errnum, errmsg)
            if num_q == 0:
                break

class CurlError(HTTPError):

    def __init__(self, errno: int, message: str) -> None:
        HTTPError.__init__(self, 599, message)
        self.errno = errno
if __name__ == '__main__':
    AsyncHTTPClient.configure(CurlAsyncHTTPClient)
    main()
