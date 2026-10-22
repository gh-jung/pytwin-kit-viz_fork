"""Exit the worker when the parent (Kit) process goes away."""

import os
import sys
import threading


def _wait_windows(pid):
    import ctypes
    from ctypes import wintypes

    SYNCHRONIZE = 0x00100000
    INFINITE = 0xFFFFFFFF
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    handle = kernel32.OpenProcess(SYNCHRONIZE, False, pid)
    if not handle:
        return  # already gone or inaccessible
    try:
        kernel32.WaitForSingleObject(handle, INFINITE)
    finally:
        kernel32.CloseHandle(handle)


def _wait_posix(pid):
    import time

    while True:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        except PermissionError:
            pass
        time.sleep(1.0)


def watch_parent(pid, on_exit):
    """Call ``on_exit()`` from a daemon thread once process ``pid`` exits."""
    wait = _wait_windows if sys.platform == "win32" else _wait_posix

    def run():
        wait(pid)
        on_exit()

    threading.Thread(target=run, name="parent-watch", daemon=True).start()
