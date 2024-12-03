"""Automatically restart the server when a source file is modified.

Most applications should not access this module directly.  Instead,
pass the keyword argument ``autoreload=True`` to the
`tornado.web.Application` constructor (or ``debug=True``, which
enables this setting and several others).  This will enable autoreload
mode as well as checking for changes to templates and static
resources.  Note that restarting is a destructive operation and any
requests in progress will be aborted when the process restarts.  (If
you want to disable autoreload while using other debug-mode features,
pass both ``debug=True`` and ``autoreload=False``).

This module can also be used as a command-line wrapper around scripts
such as unit test runners.  See the `main` method for details.

The command-line wrapper and Application debug modes can be used together.
This combination is encouraged as the wrapper catches syntax errors and
other import-time failures, while debug mode catches changes once
the server has started.

This module will not work correctly when `.HTTPServer`'s multi-process
mode is used.

Reloading loses any Python interpreter command-line arguments (e.g. ``-u``)
because it re-executes Python using ``sys.executable`` and ``sys.argv``.
Additionally, modifying these variables will cause reloading to behave
incorrectly.

"""
import os
import sys
if __name__ == '__main__':
    if sys.path[0] == os.path.dirname(__file__):
        del sys.path[0]
import functools
import importlib.abc
import os
import pkgutil
import sys
import traceback
import types
import subprocess
import weakref
from tornado import ioloop
from tornado.log import gen_log
from tornado import process
try:
    import signal
except ImportError:
    signal = None
from typing import Callable, Dict, Optional, List, Union
_has_execv = sys.platform != 'win32'
_watched_files = set()
_reload_hooks = []
_reload_attempted = False
_io_loops: 'weakref.WeakKeyDictionary[ioloop.IOLoop, bool]' = weakref.WeakKeyDictionary()
_autoreload_is_main = False
_original_argv: Optional[List[str]] = None
_original_spec = None

def start(check_time: int=500) -> None:
    """Begins watching source files for changes.

    .. versionchanged:: 5.0
       The ``io_loop`` argument (deprecated since version 4.1) has been removed.
    """
    io_loop = ioloop.IOLoop.current()
    if io_loop in _io_loops:
        return
    _io_loops[io_loop] = True
    if _autoreload_is_main:
        # If we're the main program, we reload on any changes.
        io_loop.add_callback(lambda: check_reload(io_loop, check_time))
    else:
        # Otherwise, we only check if a module has been modified and therefore
        # the process should be restarted.
        io_loop.add_callback(lambda: check_reload(io_loop, check_time, True))

def wait() -> None:
    """Wait for a watched file to change, then restart the process.

    Intended to be used at the end of scripts like unit test runners,
    to run the tests again after any source file changes (but see also
    the command-line interface in `main`)
    """
    io_loop = ioloop.IOLoop()
    io_loop.add_callback(check_reload, io_loop, 100)
    io_loop.start()

def watch(filename: str) -> None:
    """Add a file to the watch list.

    All imported modules are watched by default.
    """
    _watched_files.add(os.path.abspath(filename))

def add_reload_hook(fn: Callable[[], None]) -> None:
    """Add a function to be called before reloading the process.

    Note that for open file and socket handles it is generally
    preferable to set the ``FD_CLOEXEC`` flag (using `fcntl` or
    `os.set_inheritable`) instead of using a reload hook to close them.
    """
    _reload_hooks.append(fn)
_USAGE = '\n  python -m tornado.autoreload -m module.to.run [args...]\n  python -m tornado.autoreload path/to/script.py [args...]\n'

def main() -> None:
    """Command-line wrapper to re-run a script whenever its source changes.

    Scripts may be specified by filename or module name::

        python -m tornado.autoreload -m tornado.test.runtests
        python -m tornado.autoreload tornado/test/runtests.py

    Running a script with this wrapper is similar to calling
    `tornado.autoreload.wait` at the end of the script, but this wrapper
    can catch import-time problems like syntax errors that would otherwise
    prevent the script from reaching its call to `wait`.
    """
    global _autoreload_is_main, _original_argv, _original_spec
    _autoreload_is_main = True
    _original_argv = sys.argv
    _original_spec = sys.modules['__main__'].__spec__

    if len(sys.argv) >= 3 and sys.argv[1] == "-m":
        mode = "module"
        module = sys.argv[2]
        del sys.argv[1:3]
    elif len(sys.argv) >= 2:
        mode = "script"
        script = sys.argv[1]
        sys.argv = sys.argv[1:]
    else:
        print("Specify a module or script to run.", file=sys.stderr)
        print(_USAGE, file=sys.stderr)
        sys.exit(1)

    try:
        if mode == "module":
            import runpy
            runpy.run_module(module, run_name="__main__", alter_sys=True)
        elif mode == "script":
            with open(script) as f:
                global __file__
                __file__ = script
                # Use globals as our "locals" dictionary so that
                # something that tries to import __main__ (e.g. the unittest
                # module) will see the right things.
                exec(compile(f.read(), script, 'exec'), globals(), globals())
    except SystemExit as e:
        gen_log.info("Script exited with status %s", e.code)
    except Exception as e:
        gen_log.warning("Script exited with uncaught exception", exc_info=True)
        # If an exception occurred at import time, the file with the error
        # never made it into sys.modules and so we won't know to watch it.
        # Just to make sure we've covered everything, watch all files that
        # were accessed while we tried to load the script.
        for module in list(sys.modules.values()):
            if hasattr(module, '__file__'):
                watch(module.__file__)
    else:
        gen_log.info("Script exited normally")
    # Keep the event loop running so we can detect changes
    wait()
if __name__ == '__main__':
    main()
