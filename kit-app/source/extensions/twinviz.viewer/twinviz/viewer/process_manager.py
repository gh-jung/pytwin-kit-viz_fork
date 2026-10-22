"""Launches and supervises the out-of-process twin worker."""

import os
import subprocess
import sys
import threading
from pathlib import Path

import carb

from . import protocol as P

READY_TIMEOUT_S = 30.0


class WorkerProcess:
    """Owns the worker subprocess. Blocking parts run on helper threads."""

    def __init__(self, python_exe: Path, worker_root: Path):
        self.python_exe = Path(python_exe)
        self.worker_root = Path(worker_root)
        self.port = None
        self._proc = None
        self._ready = threading.Event()

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
        self._proc = subprocess.Popen(
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
        threading.Thread(target=self._read_stdout, args=(self._proc,), daemon=True).start()
        threading.Thread(target=self._read_stderr, args=(self._proc,), daemon=True).start()

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
        self._ready.set()  # unblock waiters if the process died early

    def _read_stderr(self, proc):
        for line in proc.stderr:
            line = line.rstrip()
            if " ERROR " in line:
                carb.log_error(line)
            elif " WARNING " in line:
                carb.log_warn(line)
            elif line:
                carb.log_info(line)
