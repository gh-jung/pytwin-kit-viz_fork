"""Benchmark how fast Kit can apply field frames, per write backend.

Run through Kit (no worker needed; data is synthetic):
    kit-app\\_build\\windows-x86_64\\release\\twinviz.app.kit.bat --exec scripts\\kit_bench.py

For each backend / representation / field kind / size, a new frame is offered
every update tick (as a fast worker would) and the script reports the mean
main-thread tick time and how many frames per second actually reached the stage.
"""

import asyncio
import os
import threading
import time

import carb
import numpy as np
import omni.kit.app
import omni.usd

from twinviz.viewer.stage_updater import StageUpdater

SIZES = [140_000, 500_000, 1_000_000]
TICKS = 40


async def bench(backend, n, display, vector):
    app = omni.kit.app.get_app()
    await omni.usd.get_context().new_stage_async()
    rng = np.random.default_rng(0)
    points = rng.random((n, 3), dtype=np.float32)
    updater = StageUpdater(backend=backend)
    updater.restyle(display=display, deform_scale=1.0 if vector else 0.0)
    updater.set_geometry({"rom": points})
    if display == "mesh":
        faces = np.stack([np.arange(n - 2), np.arange(1, n - 1), np.arange(2, n)], axis=1).astype(np.int32)
        updater.set_meshes({"rom/points": points, "rom/faces": faces, "rom/index": np.arange(n, dtype=np.int32)})
    frames = [rng.random((n, 3) if vector else n, dtype=np.float32) for _ in range(4)]
    # Warm up: first writes allocate Fabric/USD buffers.
    for i in range(6):
        updater.set_frame({"t": i}, {"rom": frames[i % 4]})
        await app.next_update_async()

    applied0 = updater.frames_applied
    tick_ms = []
    t_start = time.perf_counter()
    for i in range(TICKS):
        updater.set_frame({"t": i}, {"rom": frames[i % 4]})
        t0 = time.perf_counter()
        await app.next_update_async()
        tick_ms.append((time.perf_counter() - t0) * 1000)
    elapsed = time.perf_counter() - t_start
    fps = (updater.frames_applied - applied0) / elapsed

    updater.clear()
    updater.destroy()
    return float(np.mean(tick_ms)), fps


async def main():
    try:
        for backend in ("usd", "fabric"):
            for display in ("points", "mesh"):
                for vector in (False, True):
                    for n in SIZES:
                        tick, fps = await bench(backend, n, display, vector)
                        print(f"BENCH backend={backend:6s} display={display:6s} "
                              f"field={'vector' if vector else 'scalar':6s} n={n:>9,d}  "
                              f"tick={tick:7.2f} ms  applied={fps:6.1f} frames/s", flush=True)
    except Exception as exc:
        carb.log_error(f"[kit_bench] failed: {exc}")
        print(f"BENCH_FAILED {exc}", flush=True)
    finally:
        # post_quit alone has been seen to leave the process alive; hard-exit as a fallback.
        threading.Timer(30.0, lambda: os._exit(0)).start()
        omni.kit.app.get_app().post_quit()


asyncio.ensure_future(main())
