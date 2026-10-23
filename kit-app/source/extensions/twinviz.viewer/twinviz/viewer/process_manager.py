"""Launches and supervises the out-of-process twin worker."""

import collections
import contextlib
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import carb

from . import protocol as P

READY_TIMEOUT_S = 30.0
LOG_LINES = 200


@contextlib.contextmanager
def without_debugger_hooks():
    """Spawn processes without debugpy's subprocess injection.

    omni.kit.debug.python (part of the developer bundle) patches process creation so
    that every child Python runs under pydevd. Under that tracer the worker's Python
    code (pytwin, mesh parsing, the server loop) runs many times slower. pydevd keeps
    the original functions as ``original_<name>``; use them for the duration.
    """
    swapped = []
    targets = []
    if sys.platform == "win32":
        import _winapi

        targets.append((_winapi, "CreateProcess"))
    else:
        try:
            import _posixsubprocess

            targets.append((_posixsubprocess, "fork_exec"))
        except ImportError:
            pass
        targets.append((subprocess, "_fork_exec"))
    for module, name in targets:
        original = getattr(module, "original_" + name, None)
        if original is not None and getattr(module, name, None) is not original:
            swapped.append((module, name, getattr(module, name)))
            setattr(module, name, original)
    try:
        yield
    finally:
        for module, name, patched in swapped:
            setattr(module, name, patched)


class WorkerProcess:
    """Owns the worker subprocess. Blocking parts run on helper threads."""

    def __init__(self, python_exe: Path, worker_root: Path, debuggable=False):
        self.python_exe = Path(python_exe)
        self.debuggable = debuggable  # let omni.kit.debug.python attach to the worker
        self.worker_root = Path(worker_root)
        self.port = None
        self._proc = None
        self._ready = threading.Event()
        # (timestamp, level, text); appended from reader threads, read on the main thread.
        self.log = collections.deque(maxlen=LOG_LINES)
        self.log_version = 0

    def _log(self, level, text):
        self.log.append((time.strftime("%H:%M:%S"), level, text))
        self.log_version += 1

    @property
    def returncode(self):
        return None if self._proc is None else self._proc.poll()

    @property
    def running(self):
        return self._proc is not None and self._proc.poll() is None

    def start(self):
        if self.running:
            return
        if not self.python_exe.is_file():
            raise FileNotFoundError(
                f"worker python not found: {self.python_exe} (run scripts/setup_worker_venv.ps1)"
            )
        cmd = [
            str(self.python_exe), "-m", "twin_worker",
            "--port", "0",
            "--parent-pid", str(os.getpid()),
        ]
        creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        carb.log_info(f"[twinviz.viewer] launching worker: {cmd}")
        self.port = None
        self._ready.clear()
        hooks = contextlib.nullcontext() if self.debuggable else without_debugger_hooks()
        with hooks:
            self._proc = self._popen(cmd, creationflags)
        threading.Thread(target=self._read_stdout, args=(self._proc,), daemon=True).start()
        threading.Thread(target=self._read_stderr, args=(self._proc,), daemon=True).start()

    def _popen(self, cmd, creationflags):
        return subprocess.Popen(
            cmd,
            cwd=str(self.worker_root),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=creationflags,
        )

    def wait_ready(self, timeout=READY_TIMEOUT_S):
        """Block until the worker reports its port. Call from a thread, not the UI loop."""
        if not self._ready.wait(timeout):
            raise TimeoutError("worker did not become ready")
        if self.port is None:
            raise RuntimeError("worker exited before becoming ready")
        return self.port

    def stop(self, timeout=5.0):
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            carb.log_warn("[twinviz.viewer] worker did not exit, killing")
            proc.kill()
            proc.wait()
        carb.log_info(f"[twinviz.viewer] worker exited with {proc.returncode}")

    def _read_stdout(self, proc):
        for line in proc.stdout:
            line = line.strip()
            if line.startswith(P.READY_PREFIX):
                self.port = int(line.split("port=")[1])
                self._ready.set()
            elif line:
                carb.log_info(f"[twin_worker] {line}")
                self._log("info", line)
        self._ready.set()  # unblock waiters if the process died early
        code = proc.wait()
        self._log("info" if code == 0 else "error", f"worker exited with code {code}")

    def _read_stderr(self, proc):
        for line in proc.stderr:
            line = line.rstrip()
            if not line:
                continue
            if " ERROR " in line or line.startswith("Traceback"):
                carb.log_error(line)
                self._log("error", line)
            elif " WARNING " in line:
                carb.log_warn(line)
                self._log("warning", line)
            else:
                carb.log_info(line)
                self._log("info", line)
