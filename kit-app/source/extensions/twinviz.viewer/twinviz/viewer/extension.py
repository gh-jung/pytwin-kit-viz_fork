"""TwinViz Viewer: runs a PyTwin ROM in a separate process and visualizes it in Kit."""

import os
from pathlib import Path

import carb
import carb.settings
import omni.ext
import omni.kit.app

from .session import TwinSession
from .window import TwinVizWindow

SETTINGS_ROOT = "/exts/twinviz.viewer"


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


_instance = None


def get_session():
    """The running extension's :class:`TwinSession` (for scripts and tests)."""
    return _instance._session if _instance is not None else None


class TwinVizExtension(omni.ext.IExt):
    def on_startup(self, ext_id):
        global _instance
        _instance = self
        settings = carb.settings.get_settings()
        python_exe, worker_root = _resolve_worker_paths(ext_id)
        models_dir = settings.get(f"{SETTINGS_ROOT}/models_dir") or str(worker_root.parent / "models")
        units = settings.get(f"{SETTINGS_ROOT}/model_units_in_meters") or 1.0
        backend = settings.get(f"{SETTINGS_ROOT}/write_backend") or "fabric"

        self._session = TwinSession(python_exe, worker_root, units_in_meters=units, write_backend=backend)
        self._window = TwinVizWindow(self._session, models_dir)
        carb.log_info(f"[twinviz.viewer] worker python: {python_exe}, models: {models_dir}")

    def on_shutdown(self):
        # Kit's loop may not run again, so nothing here awaits.
        global _instance
        _instance = None
        self._window.destroy()
        self._session.destroy()
        self._window = None
        self._session = None
