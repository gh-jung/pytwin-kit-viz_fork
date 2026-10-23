"""Measure main-thread hitches in the TwinViz app while a large model loads and streams.

Runs the real extension session against scripts/fake_worker.py (synthetic data),
wraps the main-thread entry points with timers and records update-tick intervals.

    kit-app\\_build\\windows-x86_64\\release\\twinviz.app.kit.bat --exec scripts\\kit_load_bench.py
        [--/twinviz/bench/points=1000000] [--/twinviz/bench/mesh=1] [--/twinviz/bench/vector=1]
        [--/twinviz/bench/outputs=200] [--/twinviz/bench/rate=30] [--/twinviz/bench/run=6]
        [--/twinviz/bench/model=ThermalTBROM_23R2.twin]   # real worker + model instead of fake data
"""

import asyncio
import collections
import functools
import os
import subprocess
import threading
import time
from pathlib import Path

import carb
import carb.eventdispatcher
import carb.settings
import numpy as np
import omni.kit.app

import twinviz.viewer.session as session_mod
import twinviz.viewer.stage_updater as updater_mod
import twinviz.viewer.window as window_mod
from twinviz.viewer.extension import get_session
from twinviz.viewer.process_manager import without_debugger_hooks

REPO = Path(__file__).resolve().parents[1]
SECTIONS = collections.defaultdict(list)


def setting(key, default):
    value = carb.settings.get_settings().get(f"/twinviz/bench/{key}")
    return default if value in (None, "") else value


def timed(cls, name, label=None):
    original = getattr(cls, name)

    @functools.wraps(original)
    def wrapper(*args, **kwargs):
        t0 = time.perf_counter()
        try:
            return original(*args, **kwargs)
        finally:
            SECTIONS[label or f"{cls.__name__}.{name}"].append((time.perf_counter() - t0) * 1000)

    setattr(cls, name, wrapper)


class TickRecorder:
    def __init__(self):
        self.intervals = []
        self._last = None
        self._sub = carb.eventdispatcher.get_eventdispatcher().observe_event(
            event_name=omni.kit.app.GLOBAL_EVENT_UPDATE, observer_name="bench.ticks", on_event=self._tick)

    def _tick(self, _):
        now = time.perf_counter()
        if self._last is not None:
            self.intervals.append((now - self._last) * 1000)
        self._last = now

    def take(self):
        out, self.intervals = self.intervals, []
        return np.array(out or [0.0])


T0 = time.perf_counter()


def progress(msg):
    print(f"BENCH_PROGRESS {time.perf_counter() - T0:7.2f}s {msg}", flush=True)


def summary(name, ticks):
    return (f"{name} ticks={len(ticks)} p50={np.percentile(ticks, 50):.1f} p95={np.percentile(ticks, 95):.1f} "
            f"max={ticks.max():.0f} ms  hitches>50ms={int((ticks > 50).sum())}")


async def main():
    app = omni.kit.app.get_app()
    proc = None
    try:
        while get_session() is None:
            await app.next_update_async()
        session = get_session()
        for _ in range(60):
            await app.next_update_async()

        points = int(setting("points", 1_000_000))
        model = str(setting("model", ""))
        args = [str(session.worker.python_exe), str(REPO / "scripts" / "fake_worker.py"),
                "--points", str(points), "--outputs", str(setting("outputs", 200)),
                "--rate", str(setting("rate", 30))]
        if str(setting("mesh", "1")) == "1":
            args.append("--mesh")
        if str(setting("vector", "1")) == "1":
            args.append("--vector")
        if not model:
            with without_debugger_hooks():
                proc = subprocess.Popen(args, stdout=subprocess.PIPE, text=True)
            port = int(proc.stdout.readline().split("port=")[1])
            progress(f"fake worker on port {port}")
            await session.client.connect(port)
            progress("connected")

        import twinviz.viewer.client as client_mod
        import twinviz.viewer.process_manager as pm_mod

        for cls, names in ((session_mod.TwinSession, ["_on_model_info", "_on_geometry", "_on_mesh_map", "_on_frame"]),
                           (client_mod.TwinClient, ["pump", "_dispatch"]),
                           (pm_mod.WorkerProcess, ["start", "_popen"]),
                           (updater_mod.StageUpdater, ["_build", "_attach_meshes", "_commit_finished_job",
                                                       "_submit", "_remove_prims"]),
                           (window_mod.TwinVizWindow, ["_on_session_changed", "_frame_prims", "_refresh_log"])):
            for name in names:
                timed(cls, name)

        ticks = TickRecorder()
        await frames_idle(app, 30)
        ticks.take()

        t0 = time.perf_counter()
        await session.load(REPO / "models" / model if model else "fake")
        progress("load sent")
        seen = set()
        while session.updater.frames_applied == 0 or not session.updater.views:
            await app.next_update_async()
            state = (session.model_info is not None, bool(session.updater.views),
                     any(v.mesh_layer is not None for v in session.updater.views.values()),
                     session.last_frame is not None)
            if state not in seen:
                seen.add(state)
                progress(f"info={state[0]} views={state[1]} mesh={state[2]} frame={state[3]}")
            if time.perf_counter() - t0 > 240:
                raise TimeoutError("load did not finish in 240 s")
        load_s = time.perf_counter() - t0
        # Let the renderer pick up the new geometry.
        await frames_idle(app, 30)
        load_ticks = ticks.take()

        run_s = float(setting("run", 6))
        if session.mode != "dynamic":
            run_s = 0.0
        applied0 = session.updater.frames_applied
        await session.start(0.033, realtime=True)
        await asyncio.sleep(run_s)
        applied = (session.updater.frames_applied - applied0) / run_s if run_s else 0.0
        await session.pause()
        run_ticks = ticks.take()

        print(f"BENCH model={model or 'fake'} points={points} mesh={setting('mesh', '1')} vector={setting('vector', '1')} "
              f"first_frame={load_s:.2f}s applied={applied:.1f}/s", flush=True)
        print("BENCH " + summary("LOAD", load_ticks), flush=True)
        print("BENCH " + summary("RUN ", run_ticks), flush=True)
        for name, ms in sorted(SECTIONS.items(), key=lambda kv: -max(kv[1])):
            arr = np.array(ms)
            print(f"BENCH   {name:42s} n={len(arr):4d} max={arr.max():8.1f} mean={arr.mean():7.2f} ms", flush=True)
    except Exception as exc:
        carb.log_error(f"[kit_load_bench] failed: {exc}")
        print(f"BENCH_FAILED {exc}", flush=True)
    finally:
        if proc is not None:
            proc.kill()
        threading.Timer(30.0, lambda: os._exit(0)).start()
        app.post_quit()


async def frames_idle(app, n):
    for _ in range(n):
        await app.next_update_async()


asyncio.ensure_future(main())
