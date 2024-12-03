import collections
import datetime
import types
from tornado import gen, ioloop
from tornado.concurrent import Future, future_set_result_unless_cancelled
from typing import Union, Optional, Type, Any, Awaitable
import typing
if typing.TYPE_CHECKING:
    from typing import Deque, Set
__all__ = ['Condition', 'Event', 'Semaphore', 'BoundedSemaphore', 'Lock']

class _TimeoutGarbageCollector(object):
    """Base class for objects that periodically clean up timed-out waiters.

    Avoids memory leak in a common pattern like:

        while True:
            yield condition.wait(short_timeout)
            print('looping....')
    """

    def __init__(self) -> None:
        self._waiters = collections.deque()
        self._timeouts = 0

class Condition(_TimeoutGarbageCollector):
    """A condition allows one or more coroutines to wait until notified.

    Like a standard `threading.Condition`, but does not need an underlying lock
    that is acquired and released.

    With a `Condition`, coroutines can wait to be notified by other coroutines:

    .. testcode::

        import asyncio
        from tornado import gen
        from tornado.locks import Condition

        condition = Condition()

        async def waiter():
            print("I'll wait right here")
            await condition.wait()
            print("I'm done waiting")

        async def notifier():
            print("About to notify")
            condition.notify()
            print("Done notifying")

        async def runner():
            # Wait for waiter() and notifier() in parallel
            await gen.multi([waiter(), notifier()])

        asyncio.run(runner())

    .. testoutput::

        I'll wait right here
        About to notify
        Done notifying
        I'm done waiting

    `wait` takes an optional ``timeout`` argument, which is either an absolute
    timestamp::

        io_loop = IOLoop.current()

        # Wait up to 1 second for a notification.
        await condition.wait(timeout=io_loop.time() + 1)

    ...or a `datetime.timedelta` for a timeout relative to the current time::

        # Wait up to 1 second.
        await condition.wait(timeout=datetime.timedelta(seconds=1))

    The method returns False if there's no notification before the deadline.

    .. versionchanged:: 5.0
       Previously, waiters could be notified synchronously from within
       `notify`. Now, the notification will always be received on the
       next iteration of the `.IOLoop`.
    """

    def __repr__(self) -> str:
        result = '<%s' % (self.__class__.__name__,)
        if self._waiters:
            result += ' waiters[%s]' % len(self._waiters)
        return result + '>'

    def wait(self, timeout: Optional[Union[float, datetime.timedelta]]=None) -> Awaitable[bool]:
        """Wait for `.notify`.

        Returns a `.Future` that resolves ``True`` if the condition is notified,
        or ``False`` after a timeout.
        """
        future = Future()
        self._waiters.append(future)
        
        if timeout:
            if isinstance(timeout, datetime.timedelta):
                timeout = timeout.total_seconds()
            
            def on_timeout():
                self._waiters.remove(future)
                if not future.done():
                    future.set_result(False)
            
            IOLoop.current().call_later(timeout, on_timeout)
        
        return future

    def notify(self, n: int=1) -> None:
        """Wake ``n`` waiters."""
        waiters = self._waiters[:n]
        self._waiters = self._waiters[n:]
        for waiter in waiters:
            if not waiter.done():
                waiter.set_result(True)

    def notify_all(self) -> None:
        """Wake all waiters."""
        self.notify(len(self._waiters))

class Event(object):
    """An event blocks coroutines until its internal flag is set to True.

    Similar to `threading.Event`.

    A coroutine can wait for an event to be set. Once it is set, calls to
    ``yield event.wait()`` will not block unless the event has been cleared:

    .. testcode::

        import asyncio
        from tornado import gen
        from tornado.locks import Event

        event = Event()

        async def waiter():
            print("Waiting for event")
            await event.wait()
            print("Not waiting this time")
            await event.wait()
            print("Done")

        async def setter():
            print("About to set the event")
            event.set()

        async def runner():
            await gen.multi([waiter(), setter()])

        asyncio.run(runner())

    .. testoutput::

        Waiting for event
        About to set the event
        Not waiting this time
        Done
    """

    def __init__(self) -> None:
        self._value = False
        self._waiters = set()

    def __repr__(self) -> str:
        return '<%s %s>' % (self.__class__.__name__, 'set' if self.is_set() else 'clear')

    def is_set(self) -> bool:
        """Return ``True`` if the internal flag is true."""
        return self._value

    def set(self) -> None:
        """Set the internal flag to ``True``. All waiters are awakened.

        Calling `.wait` once the flag is set will not block.
        """
        if not self._value:
            self._value = True
            for waiter in self._waiters:
                if not waiter.done():
                    waiter.set_result(None)
            self._waiters.clear()

    def clear(self) -> None:
        """Reset the internal flag to ``False``.

        Calls to `.wait` will block until `.set` is called.
        """
        self._value = False

    def wait(self, timeout: Optional[Union[float, datetime.timedelta]]=None) -> Awaitable[None]:
        """Block until the internal flag is true.

        Returns an awaitable, which raises `tornado.util.TimeoutError` after a
        timeout.
        """
        if self._value:
            future = Future()
            future.set_result(None)
            return future

        waiter = Future()
        self._waiters.add(waiter)

        if timeout:
            if isinstance(timeout, datetime.timedelta):
                timeout = timeout.total_seconds()
            
            def on_timeout():
                self._waiters.discard(waiter)
                if not waiter.done():
                    waiter.set_exception(TimeoutError())
            
            IOLoop.current().call_later(timeout, on_timeout)

        return waiter

class _ReleasingContextManager(object):
    """Releases a Lock or Semaphore at the end of a "with" statement.

    with (yield semaphore.acquire()):
        pass

    # Now semaphore.release() has been called.
    """

    def __init__(self, obj: Any) -> None:
        self._obj = obj

    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type: 'Optional[Type[BaseException]]', exc_val: Optional[BaseException], exc_tb: Optional[types.TracebackType]) -> None:
        self._obj.release()

class Semaphore(_TimeoutGarbageCollector):
    """A lock that can be acquired a fixed number of times before blocking.

    A Semaphore manages a counter representing the number of `.release` calls
    minus the number of `.acquire` calls, plus an initial value. The `.acquire`
    method blocks if necessary until it can return without making the counter
    negative.

    Semaphores limit access to a shared resource. To allow access for two
    workers at a time:

    .. testsetup:: semaphore

       from collections import deque

       from tornado import gen
       from tornado.ioloop import IOLoop
       from tornado.concurrent import Future

       inited = False

       async def simulator(futures):
           for f in futures:
               # simulate the asynchronous passage of time
               await gen.sleep(0)
               await gen.sleep(0)
               if not f.done():
                   f.set_result(None)

       def use_some_resource():
           global inited
           global futures_q
           if not inited:
               inited = True
               # Ensure reliable doctest output: resolve Futures one at a time.
               futures_q = deque([Future() for _ in range(3)])
               IOLoop.current().add_callback(simulator, list(futures_q))

           return futures_q.popleft()

    .. testcode:: semaphore

        import asyncio
        from tornado import gen
        from tornado.locks import Semaphore

        sem = Semaphore(2)

        async def worker(worker_id):
            await sem.acquire()
            try:
                print("Worker %d is working" % worker_id)
                await use_some_resource()
            finally:
                print("Worker %d is done" % worker_id)
                sem.release()

        async def runner():
            # Join all workers.
            await gen.multi([worker(i) for i in range(3)])

        asyncio.run(runner())

    .. testoutput:: semaphore

        Worker 0 is working
        Worker 1 is working
        Worker 0 is done
        Worker 2 is working
        Worker 1 is done
        Worker 2 is done

    Workers 0 and 1 are allowed to run concurrently, but worker 2 waits until
    the semaphore has been released once, by worker 0.

    The semaphore can be used as an async context manager::

        async def worker(worker_id):
            async with sem:
                print("Worker %d is working" % worker_id)
                await use_some_resource()

            # Now the semaphore has been released.
            print("Worker %d is done" % worker_id)

    For compatibility with older versions of Python, `.acquire` is a
    context manager, so ``worker`` could also be written as::

        @gen.coroutine
        def worker(worker_id):
            with (yield sem.acquire()):
                print("Worker %d is working" % worker_id)
                yield use_some_resource()

            # Now the semaphore has been released.
            print("Worker %d is done" % worker_id)

    .. versionchanged:: 4.3
       Added ``async with`` support in Python 3.5.

    """

    def __init__(self, value: int=1) -> None:
        super().__init__()
        if value < 0:
            raise ValueError('semaphore initial value must be >= 0')
        self._value = value

    def __repr__(self) -> str:
        res = super().__repr__()
        extra = 'locked' if self._value == 0 else 'unlocked,value:{0}'.format(self._value)
        if self._waiters:
            extra = '{0},waiters:{1}'.format(extra, len(self._waiters))
        return '<{0} [{1}]>'.format(res[1:-1], extra)

    def release(self) -> None:
        """Increment the counter and wake one waiter."""
        self._value += 1
        while self._waiters and self._value > 0:
            waiter = self._waiters.popleft()
            if not waiter.done():
                self._value -= 1
                waiter.set_result(_ReleasingContextManager(self))
                break

    def acquire(self, timeout: Optional[Union[float, datetime.timedelta]]=None) -> Awaitable[_ReleasingContextManager]:
        """Decrement the counter. Returns an awaitable.

        Block if the counter is zero and wait for a `.release`. The awaitable
        raises `.TimeoutError` after the deadline.
        """
        if self._value > 0:
            self._value -= 1
            future = Future()
            future.set_result(_ReleasingContextManager(self))
            return future

        waiter = Future()
        self._waiters.append(waiter)

        if timeout:
            if isinstance(timeout, datetime.timedelta):
                timeout = timeout.total_seconds()
            
            def on_timeout():
                if waiter in self._waiters:
                    self._waiters.remove(waiter)
                    if not waiter.done():
                        waiter.set_exception(TimeoutError())
            
            IOLoop.current().call_later(timeout, on_timeout)

        return waiter

    def __enter__(self) -> None:
        raise RuntimeError("Use 'async with' instead of 'with' for Semaphore")

    def __exit__(self, typ: 'Optional[Type[BaseException]]', value: Optional[BaseException], traceback: Optional[types.TracebackType]) -> None:
        self.__enter__()

    async def __aenter__(self) -> None:
        await self.acquire()

    async def __aexit__(self, typ: 'Optional[Type[BaseException]]', value: Optional[BaseException], tb: Optional[types.TracebackType]) -> None:
        self.release()

class BoundedSemaphore(Semaphore):
    """A semaphore that prevents release() being called too many times.

    If `.release` would increment the semaphore's value past the initial
    value, it raises `ValueError`. Semaphores are mostly used to guard
    resources with limited capacity, so a semaphore released too many times
    is a sign of a bug.
    """

    def __init__(self, value: int=1) -> None:
        super().__init__(value=value)
        self._initial_value = value

    def release(self) -> None:
        """Increment the counter and wake one waiter."""
        if self._value >= self._initial_value:
            raise ValueError("Semaphore released too many times")
        super().release()

class Lock(object):
    """A lock for coroutines.

    A Lock begins unlocked, and `acquire` locks it immediately. While it is
    locked, a coroutine that yields `acquire` waits until another coroutine
    calls `release`.

    Releasing an unlocked lock raises `RuntimeError`.

    A Lock can be used as an async context manager with the ``async
    with`` statement:

    >>> from tornado import locks
    >>> lock = locks.Lock()
    >>>
    >>> async def f():
    ...    async with lock:
    ...        # Do something holding the lock.
    ...        await asyncio.sleep(0)  # Simulate some work
    ...
    ...    # Now the lock is released.

    For compatibility with older versions of Python, the `.acquire`
    method asynchronously returns a regular context manager:

    >>> async def f2():
    ...    with (yield lock.acquire()):
    ...        # Do something holding the lock.
    ...        await asyncio.sleep(0)  # Simulate some work
    ...
    ...    # Now the lock is released.

    .. versionchanged:: 4.3
       Added ``async with`` support in Python 3.5.

    """

    def __init__(self) -> None:
        self._block = BoundedSemaphore(value=1)

    def __repr__(self) -> str:
        return '<%s _block=%s>' % (self.__class__.__name__, self._block)

    def acquire(self, timeout: Optional[Union[float, datetime.timedelta]]=None) -> Awaitable[_ReleasingContextManager]:
        """Attempt to lock. Returns an awaitable.

        Returns an awaitable, which raises `tornado.util.TimeoutError` after a
        timeout.
        """
        return self._block.acquire(timeout)

    def release(self) -> None:
        """Unlock.

        The first coroutine in line waiting for `acquire` gets the lock.

        If not locked, raise a `RuntimeError`.
        """
        try:
            self._block.release()
        except ValueError:
            raise RuntimeError("release unlocked lock")

    def __enter__(self) -> None:
        raise RuntimeError('Use `async with` instead of `with` for Lock')

    def __exit__(self, typ: 'Optional[Type[BaseException]]', value: Optional[BaseException], tb: Optional[types.TracebackType]) -> None:
        self.__enter__()

    async def __aenter__(self) -> None:
        await self.acquire()

    async def __aexit__(self, typ: 'Optional[Type[BaseException]]', value: Optional[BaseException], tb: Optional[types.TracebackType]) -> None:
        self.release()
