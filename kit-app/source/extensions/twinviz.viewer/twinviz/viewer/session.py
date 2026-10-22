"""Glue between the worker process, the TCP client and the stage updater."""

import asyncio
import time

import carb
import numpy as np

from . import protocol as P
from .client import TwinClient
from .process_manager import WorkerProcess
from .stage_updater import StageUpdater

INPUT_SEND_INTERVAL_S = 0.05
STATIC_FRAME_TIMEOUT_S = 30.0
MAX_RESTARTS = 3            # consecutive automatic restarts before giving up
RESTART_BACKOFF_S = 1.0     # doubled after every failed attempt


class TwinSession:
    def __init__(self, python_exe, worker_root, units_in_meters=1.0, write_backend="fabric"):
        self.worker = WorkerProcess(python_exe, worker_root)
        self.client = TwinClient()
        self.updater = StageUpdater(units_in_meters, write_backend)

        self.model_info = None
        self.mesh_stats = {}
        self.running = False
        self.last_frame = None
        self.last_error = ""
        self.frames_received = 0
        self.fps = 0.0
        self.on_changed = None        # callback(what: str); what in {"connection", "model", "mesh", "frame", "state", "error"}
        self.restarts = 0             # automatic restarts since the last user action
        self.recovering = False

        # What to restore after a worker crash.
        self._load_args = None        # (path, meshes)
        self._run_args = None         # (step_size, realtime, speed) while running
        self._stopping = False        # True while the user stops the worker on purpose
        self._recover_task = None

        self._fps_count = 0
        self._fps_t0 = time.perf_counter()
        self._pending_inputs = {}
        self._input_task = None
        self._frame_event = asyncio.Event()

        c = self.client
        c.on(P.MSG_MODEL_INFO, self._on_model_info)
        c.on(P.MSG_GEOMETRY, self._on_geometry)
        c.on(P.MSG_MESH_MAP, self._on_mesh_map)
        c.on(P.MSG_FRAME, self._on_frame)
        c.on(P.MSG_STATE, self._on_state)
        c.on(P.MSG_ERROR, self._on_error)
        c.on("disconnected", self._on_disconnected)

    @property
    def connected(self):
        return self.client.connected

    @property
    def mode(self):
        return self.model_info["mode"] if self.model_info else None

    @property
    def log(self):
        return self.worker.log

    def destroy(self):
        self._stopping = True
        if self._recover_task is not None:
            self._recover_task.cancel()
        self.client.shutdown_worker_nowait()
        self.worker.stop(timeout=3.0)
        self.updater.clear()
        self.updater.destroy()

    # ---- worker lifecycle ---------------------------------------------------

    async def start_worker(self):
        if self.client.connected:
            return self.client.hello
        self.worker.start()
        port = await asyncio.get_running_loop().run_in_executor(None, self.worker.wait_ready)
        hello = await self.client.connect(port)
        self._notify("connection")
        return hello

    async def stop_worker(self):
        self._stopping = True
        self._load_args = None
        self._run_args = None
        if self._recover_task is not None:
            self._recover_task.cancel()
        await self.client.shutdown_worker()
        await asyncio.get_running_loop().run_in_executor(None, self.worker.stop)
        self.model_info = None
        self.running = False
        self.updater.clear()
        self._stopping = False
        self._notify("connection")

    async def ping(self):
        return await self.client.ping()

    # ---- commands -------------------------------------------------------------

    async def load(self, path, meshes=None):
        """Load a twin; ``meshes`` ({rom or "*": path}) overrides the model's sidecar file."""
        await self.start_worker()
        self._load_args = (path, meshes)
        self._run_args = None
        self.restarts = 0
        self.model_info = None
        self.mesh_stats = {}
        self.last_frame = None
        self.updater.clear()
        msg = {"type": P.MSG_LOAD, "path": str(path)}
        if meshes:
            msg["meshes"] = {k: str(v) for k, v in meshes.items()}
        await self.client.send(msg)

    async def start(self, step_size, realtime=True, speed=1.0):
        self._run_args = (step_size, realtime, speed)
        await self.client.send({
            "type": P.MSG_START, "step_size": step_size, "realtime": realtime, "speed": speed,
        })

    async def pause(self):
        self._run_args = None
        await self.client.send({"type": P.MSG_PAUSE})

    async def reset(self):
        self._run_args = None
        await self.client.send({"type": P.MSG_STOP})

    # ---- crash recovery -------------------------------------------------------

    def _on_disconnected(self, *_):
        self.running = False
        self._notify("connection")
        if self._stopping or self._load_args is None:
            return
        if self._recover_task is None or self._recover_task.done():
            self._recover_task = asyncio.ensure_future(self._recover())

    async def _recover(self):
        """Restart a crashed worker and restore model, inputs and run state."""
        inputs = dict(self.last_frame["inputs"]) if self.last_frame else {}
        run_args = self._run_args
        path, meshes = self._load_args
        self.recovering = True
        delay = RESTART_BACKOFF_S
        try:
            while self.restarts < MAX_RESTARTS:
                self.restarts += 1
                self.last_error = f"worker lost; restarting ({self.restarts}/{MAX_RESTARTS})"
                carb.log_warn(f"[twinviz.viewer] {self.last_error}")
                self._notify("error")
                try:
                    loop = asyncio.get_running_loop()
                    await loop.run_in_executor(None, lambda: self.worker.stop(timeout=1.0))
                    await asyncio.sleep(delay)
                    restarts = self.restarts
                    await self.load(path, meshes)
                    self.restarts = restarts  # load() resets it for user-initiated loads
                    if inputs:
                        await self.client.send({"type": P.MSG_SET_INPUTS, "inputs": inputs})
                    if run_args:
                        await self.start(*run_args)
                    self.last_error = f"worker restarted after a crash ({self.restarts}/{MAX_RESTARTS})"
                    self._notify("error")
                    return
                except Exception as exc:
                    carb.log_error(f"[twinviz.viewer] restart failed: {exc}")
                    delay *= 2
            self.last_error = f"worker keeps failing; gave up after {MAX_RESTARTS} restarts"
            self._notify("error")
        finally:
            self.recovering = False

    def set_input(self, name, value):
        """Coalesce rapid slider changes into throttled ``set_inputs`` messages."""
        self._pending_inputs[name] = float(value)
        if self._input_task is None or self._input_task.done():
            self._input_task = asyncio.ensure_future(self._flush_inputs())

    async def _flush_inputs(self):
        while self._pending_inputs and self.client.connected:
            inputs, self._pending_inputs = self._pending_inputs, {}
            self._frame_event.clear()
            await self.client.send({"type": P.MSG_SET_INPUTS, "inputs": inputs})
            if self.mode == "static":
                # Each change re-initializes the model (~0.5 s); wait for its frame
                # so a dragged slider doesn't queue up dozens of re-inits.
                try:
                    await asyncio.wait_for(self._frame_event.wait(), STATIC_FRAME_TIMEOUT_S)
                except asyncio.TimeoutError:
                    carb.log_warn("[twinviz.viewer] no frame after set_inputs")
            else:
                await asyncio.sleep(INPUT_SEND_INTERVAL_S)

    # ---- worker messages ------------------------------------------------------

    def _on_model_info(self, header, _payload):
        self.model_info = header
        self.running = False
        self._notify("model")

    def _on_geometry(self, header, payload):
        self.updater.set_geometry(P.unpack_arrays(header["arrays"], payload, np))

    def _on_mesh_map(self, header, payload):
        self.mesh_stats = header.get("stats", {})
        self.updater.set_meshes(P.unpack_arrays(header["arrays"], payload, np))
        self._notify("mesh")

    def _on_frame(self, header, payload):
        arrays = P.unpack_arrays(header["arrays"], payload, np)
        self.updater.set_frame(header, arrays)
        self.last_frame = header
        self.frames_received += 1
        self._fps_count += 1
        now = time.perf_counter()
        if now - self._fps_t0 >= 1.0:
            self.fps = self._fps_count / (now - self._fps_t0)
            self._fps_count = 0
            self._fps_t0 = now
        self._frame_event.set()
        self._notify("frame")

    def _on_state(self, header, _payload):
        self.running = header["running"]
        self._notify("state")

    def _on_error(self, header, _payload):
        self.last_error = header.get("message", "")
        self._notify("error")

    def _notify(self, what):
        if self.on_changed is not None:
            try:
                self.on_changed(what)
            except Exception as exc:
                carb.log_error(f"[twinviz.viewer] UI update failed: {exc}")
