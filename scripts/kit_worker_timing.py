"""Time worker-side operations as seen from the app: load -> first frame, input change -> frame.

    kit-app\_build\windows-x86_64\release\twinviz.app.kit.bat --exec scripts\kit_worker_timing.py
        [--/exts/twinviz.viewer/debug_worker=true]
"""

import asyncio
import os
import threading
import time
from pathlib import Path

import carb
import omni.kit.app

from twinviz.viewer.extension import get_session

REPO = Path(__file__).resolve().parents[1]


async def until(pred, timeout=180):
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
        cmd = s.worker.python_exe
        for model, inp in (("FEADeformationTBROM_23R2.twin", "Force_X_Component"),
                           ("ThermalTBROM_23R2.twin", "main_inlet_temperature")):
            applied0 = s.updater.frames_applied
            t0 = time.perf_counter()
            await s.load(REPO / "models" / model)
            await until(lambda: s.model_info is not None and s.last_frame is not None
                        and s.updater.frames_applied > applied0)
            load = time.perf_counter() - t0
            lat = []
            for v in (310.0, 320.0, 330.0):
                t0 = time.perf_counter()
                s.set_input(inp, v)
                if s.mode == "dynamic":
                    await s.start(0.05, realtime=False)
                await until(lambda: s.last_frame["inputs"].get(inp) == v)
                if s.mode == "dynamic":
                    await s.pause()
                lat.append(time.perf_counter() - t0)
            steps = "-"
            if s.mode == "dynamic":
                step0 = s.last_frame["step"]
                await s.start(0.05, realtime=False)
                await asyncio.sleep(3)
                await s.pause()
                steps = f"{(s.last_frame['step'] - step0) / 3:.0f}"
            print(f"TIMING debug_worker={s.worker.debuggable} model={model[:12]} load={load:.2f}s "
                  f"input->frame={sum(lat) / len(lat) * 1000:.0f}ms worker_steps/s={steps}", flush=True)
    except Exception as exc:
        carb.log_error(f"[kit_worker_timing] {exc!r}")
        print(f"TIMING_FAILED {exc!r}", flush=True)
    finally:
        threading.Timer(30.0, lambda: os._exit(0)).start()
        app.post_quit()


asyncio.ensure_future(main())
