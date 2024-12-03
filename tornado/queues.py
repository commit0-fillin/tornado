"""Asynchronous queues for coroutines. These classes are very similar
to those provided in the standard library's `asyncio package
<https://docs.python.org/3/library/asyncio-queue.html>`_.

.. warning::

   Unlike the standard library's `queue` module, the classes defined here
   are *not* thread-safe. To use these queues from another thread,
   use `.IOLoop.add_callback` to transfer control to the `.IOLoop` thread
   before calling any queue methods.

"""
import collections
import datetime
import heapq
from tornado import gen, ioloop
from tornado.concurrent import Future, future_set_result_unless_cancelled
from tornado.locks import Event
from typing import Union, TypeVar, Generic, Awaitable, Optional
import typing
if typing.TYPE_CHECKING:
    from typing import Deque, Tuple, Any
_T = TypeVar('_T')
__all__ = ['Queue', 'PriorityQueue', 'LifoQueue', 'QueueFull', 'QueueEmpty']

class QueueEmpty(Exception):
    """Raised by `.Queue.get_nowait` when the queue has no items."""
    pass

class QueueFull(Exception):
    """Raised by `.Queue.put_nowait` when a queue is at its maximum size."""
    pass

class _QueueIterator(Generic[_T]):

    def __init__(self, q: 'Queue[_T]') -> None:
        self.q = q

    def __anext__(self) -> Awaitable[_T]:
        return self.q.get()

class Queue(Generic[_T]):
    """Coordinate producer and consumer coroutines.

    If maxsize is 0 (the default) the queue size is unbounded.

    .. testcode::

        import asyncio
        from tornado.ioloop import IOLoop
        from tornado.queues import Queue

        q = Queue(maxsize=2)

        async def consumer():
            async for item in q:
                try:
                    print('Doing work on %s' % item)
                    await asyncio.sleep(0.01)
                finally:
                    q.task_done()

        async def producer():
            for item in range(5):
                await q.put(item)
                print('Put %s' % item)

        async def main():
            # Start consumer without waiting (since it never finishes).
            IOLoop.current().spawn_callback(consumer)
            await producer()     # Wait for producer to put all tasks.
            await q.join()       # Wait for consumer to finish all tasks.
            print('Done')

        asyncio.run(main())

    .. testoutput::

        Put 0
        Put 1
        Doing work on 0
        Put 2
        Doing work on 1
        Put 3
        Doing work on 2
        Put 4
        Doing work on 3
        Doing work on 4
        Done


    In versions of Python without native coroutines (before 3.5),
    ``consumer()`` could be written as::

        @gen.coroutine
        def consumer():
            while True:
                item = yield q.get()
                try:
                    print('Doing work on %s' % item)
                    yield gen.sleep(0.01)
                finally:
                    q.task_done()

    .. versionchanged:: 4.3
       Added ``async for`` support in Python 3.5.

    """
    _queue = None

    def __init__(self, maxsize: int=0) -> None:
        if maxsize is None:
            raise TypeError("maxsize can't be None")
        if maxsize < 0:
            raise ValueError("maxsize can't be negative")
        self._maxsize = maxsize
        self._init()
        self._getters = collections.deque([])
        self._putters = collections.deque([])
        self._unfinished_tasks = 0
        self._finished = Event()
        self._finished.set()

    @property
    def maxsize(self) -> int:
        """Number of items allowed in the queue."""
        return self._maxsize

    def qsize(self) -> int:
        """Number of items in the queue."""
        return len(self._queue)

    def put(self, item: _T, timeout: Optional[Union[float, datetime.timedelta]]=None) -> 'Future[None]':
        """Put an item into the queue, perhaps waiting until there is room.

        Returns a Future, which raises `tornado.util.TimeoutError` after a
        timeout.

        ``timeout`` may be a number denoting a time (on the same
        scale as `tornado.ioloop.IOLoop.time`, normally `time.time`), or a
        `datetime.timedelta` object for a deadline relative to the
        current time.
        """
        future = Future()
        try:
            self.put_nowait(item)
        except QueueFull:
            self._putters.append((item, future))
            IOLoop.current().add_timeout(timeout, self._put_timeout, future)
        else:
            future.set_result(None)
        return future

    def put_nowait(self, item: _T) -> None:
        """Put an item into the queue without blocking.

        If no free slot is immediately available, raise `QueueFull`.
        """
        if self.full():
            raise QueueFull()
        self._queue.append(item)
        self._notify_getters()
        self._unfinished_tasks += 1
        self._finished.clear()

    def get(self, timeout: Optional[Union[float, datetime.timedelta]]=None) -> Awaitable[_T]:
        """Remove and return an item from the queue.

        Returns an awaitable which resolves once an item is available, or raises
        `tornado.util.TimeoutError` after a timeout.

        ``timeout`` may be a number denoting a time (on the same
        scale as `tornado.ioloop.IOLoop.time`, normally `time.time`), or a
        `datetime.timedelta` object for a deadline relative to the
        current time.

        .. note::

           The ``timeout`` argument of this method differs from that
           of the standard library's `queue.Queue.get`. That method
           interprets numeric values as relative timeouts; this one
           interprets them as absolute deadlines and requires
           ``timedelta`` objects for relative timeouts (consistent
           with other timeouts in Tornado).

        """
        future = Future()
        try:
            future.set_result(self.get_nowait())
        except QueueEmpty:
            self._getters.append(future)
            IOLoop.current().add_timeout(timeout, self._get_timeout, future)
        return future

    def get_nowait(self) -> _T:
        """Remove and return an item from the queue without blocking.

        Return an item if one is immediately available, else raise
        `QueueEmpty`.
        """
        if not self._queue:
            raise QueueEmpty()
        item = self._queue.popleft()
        self._notify_putters()
        return item

    def task_done(self) -> None:
        """Indicate that a formerly enqueued task is complete.

        Used by queue consumers. For each `.get` used to fetch a task, a
        subsequent call to `.task_done` tells the queue that the processing
        on the task is complete.

        If a `.join` is blocking, it resumes when all items have been
        processed; that is, when every `.put` is matched by a `.task_done`.

        Raises `ValueError` if called more times than `.put`.
        """
        if self._unfinished_tasks <= 0:
            raise ValueError('task_done() called too many times')
        self._unfinished_tasks -= 1
        if self._unfinished_tasks == 0:
            self._finished.set()

    def join(self, timeout: Optional[Union[float, datetime.timedelta]]=None) -> Awaitable[None]:
        """Block until all items in the queue are processed.

        Returns an awaitable, which raises `tornado.util.TimeoutError` after a
        timeout.
        """
        return self._finished.wait(timeout)

    def __aiter__(self) -> _QueueIterator[_T]:
        return _QueueIterator(self)

    def __repr__(self) -> str:
        return '<%s at %s %s>' % (type(self).__name__, hex(id(self)), self._format())

    def __str__(self) -> str:
        return '<%s %s>' % (type(self).__name__, self._format())

class PriorityQueue(Queue):
    """A `.Queue` that retrieves entries in priority order, lowest first.

    Entries are typically tuples like ``(priority number, data)``.

    .. testcode::

        import asyncio
        from tornado.queues import PriorityQueue

        async def main():
            q = PriorityQueue()
            q.put((1, 'medium-priority item'))
            q.put((0, 'high-priority item'))
            q.put((10, 'low-priority item'))

            print(await q.get())
            print(await q.get())
            print(await q.get())

        asyncio.run(main())

    .. testoutput::

        (0, 'high-priority item')
        (1, 'medium-priority item')
        (10, 'low-priority item')
    """

class LifoQueue(Queue):
    """A `.Queue` that retrieves the most recently put items first.

    .. testcode::

        import asyncio
        from tornado.queues import LifoQueue

        async def main():
            q = LifoQueue()
            q.put(3)
            q.put(2)
            q.put(1)

            print(await q.get())
            print(await q.get())
            print(await q.get())

        asyncio.run(main())

    .. testoutput::

        1
        2
        3
    """
