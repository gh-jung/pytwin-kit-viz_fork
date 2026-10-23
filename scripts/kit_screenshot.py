"""Load a model in the running TwinViz app, apply inputs, capture the window, quit.

Run through Kit, e.g.:
    kit-app\\_build\\windows-x86_64\\release\\twinviz.app.kit.bat --exec "scripts\\kit_screenshot.py"

Settings (pass as --/twinviz/shot/<key>=<value>):
    model   .twin file name in models/           (default ThermalTBROM_23R2.twin)
    inputs  "name:value,name:value"               (default none)
    run     seconds to run a dynamic model        (default 0)
    deform  deform scale for vector fields        (default 0)
    settle  seconds to let the renderer settle    (default 20)
    viewport  viewport render resolution "WxH"    (default 1280x720)
    realtime  0 = step as fast as possible while running (default 1)
    report  1 = also print one short "K<n> ..." result line (see REPORT_LEGEND)
    out     output PNG path                       (default _screenshots/<model>.png)

Writes the whole app window to ``out`` and the viewport alone, rendered at
``viewport`` resolution independent of the window size, to ``<out>_viewport.png``.
"""

import asyncio
import os
import threading
import time
from pathlib import Path

import carb
import carb.settings
import numpy as np
import omni.kit.app
import omni.kit.renderer_capture

from omni.kit.viewport.utility import capture_viewport_to_file, frame_viewport_prims, get_active_viewport
from twinviz.viewer.extension import get_session
from twinviz.viewer.stage_updater import ROOT_PATH

REPO = Path(__file__).resolve().parents[1]

REPORT_LEGEND = """Kn   model number, same order as field_check.py (sorted file names); ok | ERR <error>
col  ok = rendered colors vary across the model, flat = one color, none = nothing applied
disp mesh | points (what is shown)
fps  frames/s that reached the stage while running (dynamic, run>0); '-' otherwise.
     With realtime=1 it is capped at 1/step (20); use realtime=0 to measure throughput.
rs   automatic worker restarts
we   worker error log lines
lh   load: longest app update [ms] from Load until the first frame is shown
rh   run: p95/longest app update [ms] while running ('-' if not run)"""


class TickRecorder:
    """Records intervals between app updates; long ones are what users feel as stutter."""

    def __init__(self):
        import carb.eventdispatcher

        self.intervals = []
        self._last = None
        self._sub = carb.eventdispatcher.get_eventdispatcher().observe_event(
            event_name=omni.kit.app.GLOBAL_EVENT_UPDATE, observer_name="kit_screenshot.ticks",
            on_event=self._tick)

    def _tick(self, _):
        now = time.perf_counter()
        if self._last is not None:
            self.intervals.append((now - self._last) * 1000)
        self._last = now

    def take(self):
        out, self.intervals = self.intervals, []
        return out


def model_files(folder=REPO / "models"):
    """Same numbering as field_check.py: file names sorted case-insensitively."""
    return sorted(Path(folder).glob("*.twin"), key=lambda p: (p.name.casefold(), p.name))


def report_line(session, model, run_fps, load_ticks, run_ticks):
    names = [p.name for p in model_files()]
    n = names.index(model) + 1 if model in names else 0
    updater = session.updater
    if not updater.views or updater.frames_applied == 0:
        col, disp = "none", "-"
    else:
        view = next(iter(updater.views.values()))
        use_mesh = view.active_layer(updater.style["display"]) is view.mesh_layer
        colors = updater.read_attr(view.rom, "primvars:displayColor", mesh=use_mesh)
        col = "ok" if float(np.ptp(colors, axis=0).max()) > 0.05 else "flat"
        disp = "mesh" if use_mesh else "points"
    we = sum(1 for _, level, _ in session.log if level == "error")
    fps = f"{run_fps:.0f}" if run_fps is not None else "-"
    lh = f"{max(load_ticks):.0f}" if load_ticks else "-"
    rh = f"{np.percentile(run_ticks, 95):.0f}/{max(run_ticks):.0f}" if run_ticks else "-"
    return f"K{n} ok col={col} disp={disp} fps={fps} rs={session.restarts} we={we} lh={lh} rh={rh}"


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
    report = str(setting("report", "0")) not in ("0", "", "false")
    realtime = str(setting("realtime", "1")) not in ("0", "", "false")
    vp_w, vp_h = (int(x) for x in str(setting("viewport", "1280x720")).lower().split("x"))
    run_s = float(setting("run", 0))
    deform = float(setting("deform", 0))
    out = Path(setting("out", str(REPO / "_screenshots" / (Path(model).stem + ".png"))))
    out.parent.mkdir(parents=True, exist_ok=True)

    try:
        await wait_until(lambda: get_session() is not None)
        session = get_session()
        await frames(60)  # let the viewport come up

        ticks = TickRecorder()
        await session.load(REPO / "models" / model)
        await wait_until(lambda: session.updater.views and session.updater.frames_applied > 0)
        await frames(10)  # include the renderer picking up the new geometry
        load_ticks = ticks.take()
        run_ticks = []
        for name, value in inputs.items():
            session.set_input(name, value)
        # Dynamic models apply inputs on the next step, so only static ones answer with a frame.
        if inputs and session.mode == "static":
            await wait_until(lambda: all(
                session.last_frame["inputs"].get(k) == float(v) for k, v in inputs.items()))
        if deform:
            session.updater.restyle(deform_scale=deform)
        run_fps = None
        if run_s and session.mode == "dynamic":
            await session.start(step_size=0.05, realtime=realtime)
            applied0 = session.updater.frames_applied
            ticks.take()
            await asyncio.sleep(run_s)
            run_ticks = ticks.take()
            run_fps = (session.updater.frames_applied - applied0) / run_s
            await session.pause()

        # Render the viewport at a fixed resolution (the window may be tiny without a monitor)
        # and frame the whole model, including a mesh that arrived after the points.
        viewport = get_active_viewport()
        viewport.resolution = (vp_w, vp_h)
        await frames(5)
        frame_viewport_prims(viewport, prims=[ROOT_PATH])

        # Wait for RTX to finish loading (first launch compiles shaders) and converge.
        t0 = time.perf_counter()
        while time.perf_counter() - t0 < settle_s:
            await frames(10)
        capture = omni.kit.renderer_capture.acquire_renderer_capture_interface()
        capture.capture_next_frame_swapchain(str(out))
        await frames(10)
        capture.wait_async_capture()
        vp_out = out.with_name(out.stem + "_viewport.png")
        await capture_viewport_to_file(viewport, str(vp_out)).wait_for_result()
        carb.log_warn(f"[kit_screenshot] wrote {vp_out}")
        carb.log_warn(f"[kit_screenshot] wrote {out}")
        print(f"KIT_SCREENSHOT {out}", flush=True)
        if report:
            print(report_line(session, model, run_fps, load_ticks, run_ticks), flush=True)
    except Exception as exc:
        carb.log_error(f"[kit_screenshot] failed: {exc}")
        print(f"KIT_SCREENSHOT_FAILED {exc}", flush=True)
        if report:
            names = [p.name for p in model_files()]
            n = names.index(model) + 1 if model in names else 0
            print(f"K{n} ERR {type(exc).__name__}: {str(exc)[:50]}", flush=True)
    finally:
        # post_quit alone has been seen to leave the process alive; hard-exit as a fallback.
        threading.Timer(30.0, lambda: os._exit(0)).start()
        app.post_quit()


asyncio.ensure_future(main())
