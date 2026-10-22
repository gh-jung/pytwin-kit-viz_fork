"""Load a model in the running TwinViz app, apply inputs, capture the window, quit.

Run through Kit, e.g.:
    kit-app\\_build\\windows-x86_64\\release\\twinviz.app.kit.bat --exec "scripts\\kit_screenshot.py"

Settings (pass as --/twinviz/shot/<key>=<value>):
    model   .twin file name in models/           (default ThermalTBROM_23R2.twin)
    inputs  "name:value,name:value"               (default none)
    run     seconds to run a dynamic model        (default 0)
    deform  deform scale for vector fields        (default 0)
    settle  seconds to let the renderer settle    (default 20)
    out     output PNG path                       (default _screenshots/<model>.png)
"""

import asyncio
import os
import threading
import time
from pathlib import Path

import carb
import carb.settings
import omni.kit.app
import omni.kit.renderer_capture

from twinviz.viewer.extension import get_session

REPO = Path(__file__).resolve().parents[1]


def setting(key, default):
    value = carb.settings.get_settings().get(f"/twinviz/shot/{key}")
    return default if value in (None, "") else value


async def wait_until(predicate, timeout=120.0):
    app = omni.kit.app.get_app()
    t0 = time.perf_counter()
    while not predicate():
        if time.perf_counter() - t0 > timeout:
            raise TimeoutError("condition not met")
        await app.next_update_async()


async def frames(n):
    for _ in range(n):
        await omni.kit.app.get_app().next_update_async()


async def main():
    app = omni.kit.app.get_app()
    model = setting("model", "ThermalTBROM_23R2.twin")
    # Not JSON: Kit tries to parse JSON-looking command-line values itself.
    inputs = {k: float(v) for k, v in
              (kv.split(":") for kv in str(setting("inputs", "")).split(",") if kv)}
    settle_s = float(setting("settle", 20))
    run_s = float(setting("run", 0))
    deform = float(setting("deform", 0))
    out = Path(setting("out", str(REPO / "_screenshots" / (Path(model).stem + ".png"))))
    out.parent.mkdir(parents=True, exist_ok=True)

    try:
        await wait_until(lambda: get_session() is not None)
        session = get_session()
        await frames(60)  # let the viewport come up

        await session.load(REPO / "models" / model)
        await wait_until(lambda: session.updater.views and session.updater.frames_applied > 0)
        for name, value in inputs.items():
            session.set_input(name, value)
        # Dynamic models apply inputs on the next step, so only static ones answer with a frame.
        if inputs and session.mode == "static":
            await wait_until(lambda: all(
                session.last_frame["inputs"].get(k) == float(v) for k, v in inputs.items()))
        if deform:
            session.updater.restyle(deform_scale=deform)
        if run_s and session.mode == "dynamic":
            await session.start(step_size=0.05, realtime=True)
            await asyncio.sleep(run_s)
            await session.pause()

        # Wait for RTX to finish loading (first launch compiles shaders) and converge.
        t0 = time.perf_counter()
        while time.perf_counter() - t0 < settle_s:
            await frames(10)
        capture = omni.kit.renderer_capture.acquire_renderer_capture_interface()
        capture.capture_next_frame_swapchain(str(out))
        await frames(10)
        capture.wait_async_capture()
        carb.log_warn(f"[kit_screenshot] wrote {out}")
        print(f"KIT_SCREENSHOT {out}", flush=True)
    except Exception as exc:
        carb.log_error(f"[kit_screenshot] failed: {exc}")
        print(f"KIT_SCREENSHOT_FAILED {exc}", flush=True)
    finally:
        # post_quit alone has been seen to leave the process alive; hard-exit as a fallback.
        threading.Timer(30.0, lambda: os._exit(0)).start()
        app.post_quit()


asyncio.ensure_future(main())
