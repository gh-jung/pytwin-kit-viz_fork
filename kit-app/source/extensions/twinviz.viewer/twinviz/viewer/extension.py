"""TwinViz Viewer: runs a PyTwin ROM in a separate process and visualizes it in Kit."""

import asyncio
import os
from pathlib import Path

import carb
import carb.settings
import omni.ext
import omni.kit.app
import omni.ui as ui

from .client import TwinClient
from .process_manager import WorkerProcess

SETTINGS_ROOT = "/exts/twinviz.viewer"
WINDOW_TITLE = "TwinViz Viewer"


def _find_default_worker_root(ext_id):
    """Walk up from the extension folder to the repo's ``twin_worker`` directory.

    The extension may run from ``kit-app/source/extensions`` or from a link
    under ``kit-app/_build/...``, so the depth is not fixed.
    """
    ext_path = omni.kit.app.get_app().get_extension_manager().get_extension_path(ext_id)
    start = Path(os.path.realpath(ext_path))
    for parent in start.parents:
        candidate = parent / "twin_worker"
        if (candidate / "pyproject.toml").is_file():
            return candidate
    raise FileNotFoundError(
        f"twin_worker not found above {start}; set {SETTINGS_ROOT}/worker_root"
    )


def _resolve_worker_paths(ext_id):
    """Return ``(python_exe, worker_root)`` from settings or repository layout."""
    settings = carb.settings.get_settings()
    worker_root = settings.get(f"{SETTINGS_ROOT}/worker_root") or str(_find_default_worker_root(ext_id))
    python_exe = settings.get(f"{SETTINGS_ROOT}/worker_python") or str(
        Path(worker_root) / ".venv" / "Scripts" / "python.exe"
    )
    return Path(python_exe), Path(worker_root)


class TwinVizExtension(omni.ext.IExt):
    def on_startup(self, ext_id):
        python_exe, worker_root = _resolve_worker_paths(ext_id)
        self._worker = WorkerProcess(python_exe, worker_root)
        self._client = TwinClient()
        self._client.on("disconnected", lambda *_: self._set_status("disconnected"))
        self._busy = False
        self._build_ui()
        carb.log_info(f"[twinviz.viewer] worker python: {python_exe}")

    def on_shutdown(self):
        # Kit's loop may not run again, so don't await here; stop() kills the
        # worker if the shutdown message never arrives.
        self._client.shutdown_worker_nowait()
        self._worker.stop(timeout=3.0)
        self._window = None

    # ---- UI -------------------------------------------------------------

    def _build_ui(self):
        self._window = ui.Window(WINDOW_TITLE, width=360, height=180)
        with self._window.frame:
            with ui.VStack(spacing=6, height=0):
                with ui.HStack(height=24):
                    ui.Label("Status:", width=60)
                    self._status_label = ui.Label("stopped", name="status")
                with ui.HStack(height=28, spacing=4):
                    ui.Button("Start Worker", clicked_fn=lambda: self._run(self._start()))
                    ui.Button("Ping", clicked_fn=lambda: self._run(self._ping()))
                    ui.Button("Stop Worker", clicked_fn=lambda: self._run(self._stop()))
                self._info_label = ui.Label("", word_wrap=True, name="info")

    def _set_status(self, text, info=None):
        self._status_label.text = text
        if info is not None:
            self._info_label.text = info

    def _run(self, coro):
        if self._busy:
            coro.close()
            return

        async def guarded():
            self._busy = True
            try:
                await coro
            except Exception as exc:
                carb.log_error(f"[twinviz.viewer] {exc}")
                self._set_status("error", str(exc))
            finally:
                self._busy = False

        asyncio.ensure_future(guarded())

    # ---- actions --------------------------------------------------------

    async def _start(self):
        if self._client.connected:
            return
        self._set_status("starting...", "")
        self._worker.start()
        port = await asyncio.get_running_loop().run_in_executor(None, self._worker.wait_ready)
        hello = await self._client.connect(port)
        self._set_status(
            "connected",
            f"worker pid={hello['pid']} v{hello['worker_version']} port={port}",
        )

    async def _ping(self):
        if not self._client.connected:
            self._set_status("not connected")
            return
        latency = await self._client.ping()
        self._set_status("connected", f"pong in {latency * 1000:.2f} ms")

    async def _stop(self):
        await self._client.shutdown_worker()
        await asyncio.get_running_loop().run_in_executor(None, self._worker.stop)
        self._set_status("stopped", "")
