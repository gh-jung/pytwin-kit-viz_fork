"""Where does streamline time go? Worker compute vs Kit USD write vs render.

    kit-app\\_build\\windows-x86_64\\release\\twinviz.app.kit.bat --exec scripts\\kit_stream_bench.py
        [--/twinviz/bench/model=HXVelVectorTBROM_23R2.twin] [--/twinviz/bench/seeds=100,300,1000]

For each seed count: end-to-end latency (set_post -> prims updated), the worker's
compute time (from its log), the Kit-side overlay write, and the app update times
in the second after the curves appear (renderer picking up the new geometry).
"""

import asyncio
import functools
import os
import threading
import time
from pathlib import Path

import carb
import carb.eventdispatcher
import carb.settings
import numpy as np
import omni.kit.app
import omni.usd
from pxr import UsdGeom

import twinviz.viewer.overlays as overlays_mod
from twinviz.viewer.extension import get_session

REPO = Path(__file__).resolve().parents[1]
APPLY_MS = []


def setting(key, default):
    value = carb.settings.get_settings().get(f"/twinviz/bench/{key}")
    return default if value in (None, "") else value


def timed_apply():
    original = overlays_mod.Overlays.apply

    @functools.wraps(original)
    def wrapper(self, *args, **kwargs):
        t0 = time.perf_counter()
        try:
            return original(self, *args, **kwargs)
        finally:
            APPLY_MS.append((time.perf_counter() - t0) * 1000)

    overlays_mod.Overlays.apply = wrapper


class Ticks:
    def __init__(self):
        self.t = []
        self._last = None
        self._sub = carb.eventdispatcher.get_eventdispatcher().observe_event(
            event_name=omni.kit.app.GLOBAL_EVENT_UPDATE, observer_name="bench.ticks", on_event=self._tick)

    def _tick(self, _):
        now = time.perf_counter()
        if self._last is not None:
            self.t.append((now - self._last) * 1000)
        self._last = now

    def take(self):
        out, self.t = self.t, []
        return np.array(out or [0.0])


async def until(pred, timeout=300):
    app = omni.kit.app.get_app()
    t0 = time.perf_counter()
    while not pred():
        if time.perf_counter() - t0 > timeout:
            raise TimeoutError
        await app.next_update_async()


async def main():
    app = omni.kit.app.get_app()
    try:
        await until(lambda: get_session() is not None)
        s = get_session()
        timed_apply()
        model = str(setting("model", "HXVelVectorTBROM_23R2.twin"))
        applied0 = s.updater.frames_applied
        await s.load(REPO / "models" / model)
        await until(lambda: s.model_info is not None and s.updater.frames_applied > applied0)
        s.updater.restyle(display="none")
        for name, value in (("Mass_Flow_HX", 1.0), ("Tube_temperature", 350.0), ("shell_inlet_temp", 300.0)):
            if s.model_info and name in s.model_info["inputs"]:
                s.set_input(name, value)
        await s.start(0.5, realtime=False)
        await asyncio.sleep(1.0)
        await s.pause()
        # Warm up: the first request builds the voxel grid (one-off).
        await s.set_post(streamlines=10, seed="inlet")
        await until(lambda: s.updater.overlays.last_items)
        ticks = Ticks()

        for seeds in [int(x) for x in str(setting("seeds", "100,300,1000")).split(",")]:
            posts0 = s.updater.posts_applied
            APPLY_MS.clear()
            t0 = time.perf_counter()
            await s.set_post(streamlines=seeds)
            await until(lambda: s.updater.posts_applied > posts0)
            latency = time.perf_counter() - t0
            ticks.take()
            for _ in range(60):
                await app.next_update_async()
            after = ticks.take()
            items = s.updater.overlays.last_items
            path = next(iter(s.updater.overlays.paths.values()), None)
            n_pts = 0
            if path:
                prim = omni.usd.get_context().get_stage().GetPrimAtPath(path)
                n_pts = len(UsdGeom.BasisCurves(prim).GetPointsAttr().Get() or [])
            print(f"STREAM seeds={seeds:5d} points={n_pts:8d} latency={latency * 1000:7.0f} ms "
                  f"kit_write={max(APPLY_MS or [0]):6.1f} ms  after: p50={np.percentile(after, 50):5.1f} "
                  f"max={after.max():6.1f} ms  items={len(items)}", flush=True)

        # While stepping: streamlines refresh with every post update.
        await s.set_post(streamlines=300)
        ticks.take()
        posts0 = s.updater.posts_applied
        await s.start(0.5, realtime=False)
        await asyncio.sleep(5)
        await s.pause()
        run = ticks.take()
        print(f"STREAM running seeds=300: post updates={(s.updater.posts_applied - posts0) / 5:.1f}/s "
              f"tick p50={np.percentile(run, 50):.1f} p95={np.percentile(run, 95):.1f} max={run.max():.0f} ms",
              flush=True)
        for _, level, text in list(s.log)[-200:]:
            if "post" in text and "ms" in text:
                print("STREAM log", text[-120:], flush=True)
    except Exception as exc:
        carb.log_error(f"[kit_stream_bench] {exc!r}")
        print(f"STREAM_FAILED {exc!r}", flush=True)
    finally:
        threading.Timer(30.0, lambda: os._exit(0)).start()
        app.post_quit()


asyncio.ensure_future(main())
