"""Owns the TwinModel on a dedicated thread and produces frames.

All pytwin calls happen on the runner thread; the asyncio server talks to it
only through ``submit()`` (commands in) and the ``emit`` callback (messages out).

Two evaluation modes, chosen per model at load time:

* ``dynamic`` — every TBROM exposes its mode coefficients as twin outputs, so
  the model is stepped in time and ``generate_snapshot`` runs after each step.
* ``static`` — at least one TBROM only writes snapshots to disk at
  initialization (``<rom>_store_snapshots``). Time stepping does not update the
  field, so an input change re-initializes the model and reads the snapshot file.
"""

import logging
import queue
import threading
import time
from pathlib import Path

import numpy as np

from . import protocol as P

log = logging.getLogger(__name__)

MODE_DYNAMIC = "dynamic"
MODE_STATIC = "static"


def _py(v):
    """numpy scalars/strings -> JSON-serializable python values."""
    if isinstance(v, (np.floating, np.integer)):
        return v.item()
    if isinstance(v, np.str_):
        return str(v)
    return v


def _plain_dict(d):
    return {str(k): _py(v) for k, v in d.items()}


class TwinRunner:
    def __init__(self, emit):
        """``emit(header, payload=b"", latest_only=False)`` is called from the runner thread."""
        self._emit = emit
        self._commands = queue.Queue()
        self._thread = threading.Thread(target=self._run, name="twin-runner", daemon=True)

        self._tm = None
        self._mode = None
        self._store_params = {}
        self._inputs = {}
        self._parameters = {}
        self._inputs_dirty = False
        self._running = False
        self._step = 0
        self._step_size = 0.1
        self._realtime = True
        self._speed = 1.0
        self._next_wall = 0.0

    # ---- public (any thread) -------------------------------------------

    def start(self):
        self._thread.start()

    def submit(self, header):
        self._commands.put(header)

    def close(self):
        self._commands.put({"type": "_quit"})
        self._thread.join(timeout=10)

    # ---- runner thread ---------------------------------------------------

    def _run(self):
        while True:
            # Block for a command while idle; poll without blocking while stepping.
            # Static models never step: their fields change only on re-initialization.
            stepping = self._running and self._mode == MODE_DYNAMIC
            try:
                cmd = self._commands.get() if not stepping else self._commands.get_nowait()
            except queue.Empty:
                cmd = None
            if cmd is not None:
                if cmd["type"] == "_quit":
                    self._unload()
                    return
                try:
                    self._handle(cmd)
                except Exception as exc:
                    log.exception("command %s failed", cmd.get("type"))
                    self._running = False
                    self._emit({"type": P.MSG_ERROR, "message": f"{cmd.get('type')}: {exc}"})
                continue
            if stepping:
                try:
                    self._tick()
                except Exception as exc:
                    log.exception("evaluation failed")
                    self._running = False
                    self._emit({"type": P.MSG_ERROR, "message": f"evaluation: {exc}"})

    def _handle(self, cmd):
        t = cmd["type"]
        if t == P.MSG_LOAD:
            self._load(cmd["path"])
        elif self._tm is None:
            raise RuntimeError("no model loaded")
        elif t == P.MSG_INIT:
            self._inputs.update(cmd.get("inputs") or {})
            self._parameters.update(cmd.get("parameters") or {})
            self._initialize()
        elif t == P.MSG_START:
            self._step_size = float(cmd.get("step_size", self._step_size))
            self._realtime = bool(cmd.get("realtime", self._realtime))
            self._speed = float(cmd.get("speed", self._speed))
            self._running = True
            self._next_wall = time.perf_counter()
            self._emit_state()
        elif t == P.MSG_PAUSE:
            self._running = False
            self._emit_state()
        elif t == P.MSG_SET_INPUTS:
            unknown = set(cmd["inputs"]) - set(self._inputs)
            if unknown:
                raise KeyError(f"unknown inputs: {sorted(unknown)}")
            self._inputs.update({k: float(v) for k, v in cmd["inputs"].items()})
            self._inputs_dirty = True
            if self._mode == MODE_STATIC:
                # Static fields only change on re-initialization.
                self._initialize()
        elif t == P.MSG_STOP:
            self._running = False
            self._initialize()
        else:
            raise ValueError(f"unsupported command: {t}")

    # ---- model lifecycle -----------------------------------------------------

    def _load(self, path):
        from pytwin import TwinModel  # heavy import, only on the runner thread

        path = Path(path)
        if not path.is_file():
            raise FileNotFoundError(path)
        self._unload()
        log.info("loading %s", path)
        t0 = time.perf_counter()
        self._tm = TwinModel(str(path))
        tm = self._tm
        self._inputs = _plain_dict(tm.inputs)
        self._parameters = {}
        self._store_params = {
            str(p): 1 for p in tm.parameters if str(p).endswith("_store_snapshots")
        }
        tm.initialize_evaluation(parameters=self._store_params or None)
        self._mode = MODE_DYNAMIC
        for rom in tm.tbrom_names:
            if not self._has_output_modes(rom):
                if not self._store_params:
                    raise RuntimeError(f"TBROM {rom} exposes neither mode outputs nor stored snapshots")
                self._mode = MODE_STATIC
        log.info("loaded in %.2fs, mode=%s", time.perf_counter() - t0, self._mode)

        self._emit(self._model_info(path))
        self._emit_geometry()
        self._initialize()

    def _has_output_modes(self, rom):
        """Whether the TBROM's mode coefficients are twin outputs (in-memory snapshots work)."""
        # pytwin has no public flag; probing generate_snapshot works but logs an ERROR.
        tbrom = getattr(self._tm, "_tbroms", {}).get(rom)
        flag = getattr(tbrom, "_hasoutmcs", None)
        if flag is not None:
            return bool(flag)
        try:
            self._tm.generate_snapshot(rom, on_disk=False)
            return True
        except Exception:
            return False

    def _unload(self):
        self._running = False
        if self._tm is not None:
            try:
                self._tm.close()
            except Exception:
                log.exception("closing twin model")
            self._tm = None

    def _initialize(self):
        params = dict(self._parameters)
        params.update(self._store_params)
        self._tm.initialize_evaluation(parameters=params or None, inputs=self._inputs)
        self._step = 0
        self._inputs_dirty = False
        self._emit_frame()

    def _tick(self):
        inputs = self._inputs if self._inputs_dirty else None
        self._tm.evaluate_step_by_step(step_size=self._step_size, inputs=inputs)
        self._inputs_dirty = False
        self._step += 1
        self._emit_frame()
        if self._realtime:
            self._next_wall += self._step_size / self._speed
            delay = self._next_wall - time.perf_counter()
            if delay > 0:
                time.sleep(delay)
            else:
                self._next_wall = time.perf_counter()  # fell behind; don't try to catch up

    # ---- messages ----------------------------------------------------------

    def _model_info(self, path):
        tm = self._tm
        roms = []
        for rom in tm.tbrom_names:
            roms.append({
                "name": rom,
                "field": _py(tm.get_field_output_name(rom)),
                "location": _py(tm.get_tbrom_data_location(rom)),
                "field_inputs": [_py(x) for x in tm.get_field_input_names(rom)],
                "named_selections": [_py(x) for x in tm.get_named_selections(rom)],
            })
        return {
            "type": P.MSG_MODEL_INFO,
            "path": str(path),
            "name": path.stem,
            "mode": self._mode,
            "inputs": _plain_dict(tm.inputs),
            "outputs": [str(k) for k in tm.outputs],
            "parameters": _plain_dict(tm.parameters),
            "tbroms": roms,
        }

    def _emit_geometry(self):
        arrays = {}
        for rom in self._tm.tbrom_names:
            pts = np.asarray(self._tm.generate_points(rom, on_disk=False), dtype=np.float32)
            arrays[rom] = pts.reshape(-1, 3)
        meta, payload = P.pack_arrays(arrays)
        self._emit({"type": P.MSG_GEOMETRY, "arrays": meta}, payload)

    def _snapshot(self, rom):
        tm = self._tm
        if self._mode == MODE_DYNAMIC:
            data = tm.generate_snapshot(rom, on_disk=False)
        else:
            from pytwin import read_binary

            data = read_binary(tm.get_snapshot_filepath(rom, tm.evaluation_time))
        return np.asarray(data, dtype=np.float32)

    def _emit_frame(self):
        tm = self._tm
        arrays = {rom: self._snapshot(rom) for rom in tm.tbrom_names}
        meta, payload = P.pack_arrays(arrays)
        header = {
            "type": P.MSG_FRAME,
            "t": float(tm.evaluation_time),
            "step": self._step,
            "inputs": dict(self._inputs),
            "scalars": _plain_dict(tm.outputs),
            "arrays": meta,
        }
        self._emit(header, payload, latest_only=True)

    def _emit_state(self):
        self._emit({"type": P.MSG_STATE, "running": self._running, "mode": self._mode})
