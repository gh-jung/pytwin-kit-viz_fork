"""omni.ui window for TwinViz."""

import asyncio
import time
from pathlib import Path

import carb
import carb.eventdispatcher
import omni.kit.app
import omni.ui as ui
import omni.usd
from omni.kit.window.filepicker import FilePickerDialog

from . import colormap
from .stage_updater import DISPLAY_MESH, DISPLAY_NONE, DISPLAY_POINTS

WINDOW_TITLE = "TwinViz Viewer"
LABEL_W = 110
UI_REFRESH_S = 0.2


class TwinVizWindow:
    def __init__(self, session, models_dir):
        self._session = session
        self._models_dir = Path(models_dir)
        self._model_files = []
        self._busy = False
        self._last_ui_refresh = 0.0
        self._output_labels = {}
        self._usd_dialog = None

        session.on_changed = self._on_session_changed
        session.updater.on_geometry_built = self._frame_prims

        self._window = ui.Window(WINDOW_TITLE, width=420, height=720)
        # Tab next to the Property panel instead of floating over the viewport.
        self._window.deferred_dock_in("Property", ui.DockPolicy.CURRENT_WINDOW_IS_ACTIVE)
        with self._window.frame:
            with ui.ScrollingFrame():
                with ui.VStack(spacing=6, height=0):
                    self._build_worker_section()
                    self._build_model_section()
                    self._build_usd_section()
                    self._build_run_section()
                    self._inputs_frame = ui.CollapsableFrame("Inputs", build_fn=self._build_inputs)
                    self._outputs_frame = ui.CollapsableFrame("Outputs", build_fn=self._build_outputs)
                    self._build_display_section()
                    self._build_log_section()
        self._refresh_models()
        self._log_version = -1
        self._log_sub = carb.eventdispatcher.get_eventdispatcher().observe_event(
            event_name=omni.kit.app.GLOBAL_EVENT_UPDATE,
            observer_name="twinviz.viewer.LogPanel",
            on_event=lambda _: self._refresh_log(),
        )

    def destroy(self):
        if self._usd_dialog is not None:
            self._usd_dialog.destroy()
            self._usd_dialog = None
        self._log_sub = None
        self._session.on_changed = None
        self._session.updater.on_geometry_built = None
        if self._window:
            self._window.destroy()
        self._window = None

    # ---- sections ---------------------------------------------------------------

    def _build_worker_section(self):
        with ui.CollapsableFrame("Worker"):
            with ui.VStack(spacing=4, height=0):
                with ui.HStack(height=22):
                    ui.Label("Status", width=LABEL_W)
                    self._status_label = ui.Label("stopped")
                with ui.HStack(height=26, spacing=4):
                    ui.Button("Start Worker", clicked_fn=lambda: self._run(self._session.start_worker()))
                    ui.Button("Ping", clicked_fn=lambda: self._run(self._ping()))
                    ui.Button("Stop Worker", clicked_fn=lambda: self._run(self._session.stop_worker()))
                self._error_label = ui.Label("", word_wrap=True, style={"color": 0xFF5555FF})

    def _build_model_section(self):
        with ui.CollapsableFrame("Model"):
            with ui.VStack(spacing=4, height=0):
                with ui.HStack(height=24, spacing=4):
                    self._model_combo_frame = ui.Frame(build_fn=self._build_model_combo)
                    ui.Button("Refresh", width=70, clicked_fn=self._refresh_models)
                ui.Button("Load", height=26, clicked_fn=self._on_load)
                self._model_label = ui.Label("no model", word_wrap=True)

    def _build_model_combo(self):
        names = [p.name for p in self._model_files] or ["(no .twin in models/)"]
        self._model_combo = ui.ComboBox(0, *names)

    def _build_usd_section(self):
        with ui.CollapsableFrame("USD Scene"):
            with ui.VStack(spacing=4, height=0):
                with ui.HStack(height=24, spacing=4):
                    self._usd_path = ui.StringField()
                    ui.Button("Browse", width=70, clicked_fn=self._browse_usd)
                ui.Button("Load USD", height=26,
                          clicked_fn=lambda: self._run(self._load_usd()))
                ui.Label("Opens a new stage. All BasisCurves purpose values become default.",
                         word_wrap=True)
                self._usd_label = ui.Label("no USD scene", word_wrap=True)

    def _browse_usd(self):
        if self._usd_dialog is None:
            self._usd_dialog = FilePickerDialog(
                "Open USD Scene", apply_button_label="Open",
                click_apply_handler=self._select_usd,
                click_cancel_handler=lambda *_: self._usd_dialog.hide(),
                item_filter_fn=lambda item: not item or item.is_folder or
                    item.path.lower().endswith((".usd", ".usda", ".usdc", ".usdz")),
            )
        self._usd_dialog.show()

    def _select_usd(self, filename, dirname):
        directory = dirname.replace("\\", "/").rstrip("/")
        self._usd_path.model.set_value(f"{directory}/{filename}" if directory else filename)
        self._usd_dialog.hide()
        self._run(self._load_usd())

    async def _load_usd(self):
        from .usd_loader import default_basis_curves

        path = self._usd_path.model.as_string.strip().strip('"')
        if not path.lower().endswith((".usd", ".usda", ".usdc", ".usdz")):
            raise ValueError("Select a .usd, .usda, .usdc or .usdz file")
        # Stop old frames before replacing their stage (also disables recovery).
        await self._session.stop_worker()
        self._session.last_frame = None
        self._session.mesh_stats = {}
        self._inputs_frame.rebuild()
        self._outputs_frame.rebuild()
        self._seed_frame.rebuild()
        self._model_label.text = "no twin model"
        self._frame_label.text = ""
        self._range_label.text = ""
        self._usd_label.text = f"loading {path}..."
        context = omni.usd.get_context()
        success, error = await context.open_stage_async(path)
        if not success:
            self._usd_label.text = "USD load failed"
            raise RuntimeError(f"Could not open USD: {error}")
        stage = context.get_stage()
        paths, expanded = default_basis_curves(stage)
        self._usd_label.text = (f"{path}\nBasisCurves: {len(paths)}; purpose: default"
                                f"\nExpanded curve instances: {expanded}")
        carb.log_info(f"[twinviz.viewer] USD loaded: {len(paths)} BasisCurves set to default")
        self._frame_prims([str(p.GetPath()) for p in stage.GetPseudoRoot().GetChildren()])

    def _build_run_section(self):
        with ui.CollapsableFrame("Run"):
            with ui.VStack(spacing=4, height=0):
                with ui.HStack(height=22):
                    ui.Label("Step size [s]", width=LABEL_W)
                    self._step_field = ui.FloatField()
                    self._step_field.model.set_value(0.05)
                with ui.HStack(height=22):
                    ui.Label("Real time", width=LABEL_W)
                    self._realtime_box = ui.CheckBox()
                    self._realtime_box.model.set_value(True)
                with ui.HStack(height=26, spacing=4):
                    ui.Button("Start", clicked_fn=lambda: self._run(self._session.start(
                        self._step_field.model.as_float, self._realtime_box.model.as_bool)))
                    ui.Button("Pause", clicked_fn=lambda: self._run(self._session.pause()))
                    ui.Button("Reset", clicked_fn=lambda: self._run(self._session.reset()))
                self._frame_label = ui.Label("")

    def _build_inputs(self):
        info = self._session.model_info
        with ui.VStack(spacing=4, height=0):
            if not info or not info["inputs"]:
                ui.Label("no inputs")
                return
            if info["mode"] == "dynamic":
                ui.Label("Applied on the next time step (press Start).", style={"color": 0xFF999999})
            frame = self._session.last_frame
            current = frame["inputs"] if frame else info["inputs"]
            for name, default in info["inputs"].items():
                with ui.HStack(height=22):
                    ui.Label(name, width=LABEL_W * 1.6, elided_text=True, tooltip=name)
                    drag = ui.FloatDrag(step=max(abs(default) * 0.01, 0.1))
                    drag.model.set_value(float(current.get(name, default)))
                    drag.model.add_value_changed_fn(
                        lambda m, n=name: self._session.set_input(n, m.as_float))

    def _build_outputs(self):
        self._output_labels = {}
        info = self._session.model_info
        with ui.VStack(spacing=2, height=0):
            if not info:
                ui.Label("no outputs")
                return
            for name in info["outputs"][:40]:
                with ui.HStack(height=20):
                    ui.Label(name, width=LABEL_W * 1.6, elided_text=True, tooltip=name)
                    self._output_labels[name] = ui.Label("-")

    def _build_display_section(self):
        style = self._session.updater.style
        with ui.CollapsableFrame("Display"):
            with ui.VStack(spacing=4, height=0):
                with ui.HStack(height=22):
                    ui.Label("Show", width=LABEL_W, tooltip="Mesh needs a <model>.twinviz.json sidecar")
                    displays = [DISPLAY_MESH, DISPLAY_POINTS, DISPLAY_NONE]
                    show = ui.ComboBox(displays.index(style["display"]), *displays)
                    show.model.get_item_value_model().add_value_changed_fn(
                        lambda m: self._restyle(display=displays[m.as_int]))
                with ui.HStack(height=22):
                    ui.Label("Colormap", width=LABEL_W)
                    combo = ui.ComboBox(colormap.NAMES.index(style["colormap"]), *colormap.NAMES)
                    combo.model.get_item_value_model().add_value_changed_fn(
                        lambda m: self._restyle(colormap=colormap.NAMES[m.as_int]))
                with ui.HStack(height=22):
                    ui.Label("Auto range", width=LABEL_W)
                    auto = ui.CheckBox()
                    auto.model.set_value(style["auto_range"])
                    auto.model.add_value_changed_fn(lambda m: self._restyle(auto_range=m.as_bool))
                with ui.HStack(height=22, spacing=4):
                    ui.Label("Min / Max", width=LABEL_W)
                    self._vmin_field = ui.FloatField()
                    self._vmax_field = ui.FloatField()
                    self._vmin_field.model.add_value_changed_fn(lambda m: self._restyle(vmin=m.as_float))
                    self._vmax_field.model.add_value_changed_fn(lambda m: self._restyle(vmax=m.as_float))
                with ui.HStack(height=22):
                    ui.Label("Deform scale", width=LABEL_W, tooltip="Vector fields only")
                    deform = ui.FloatDrag(min=0.0, step=1.0)
                    deform.model.add_value_changed_fn(lambda m: self._restyle(deform_scale=m.as_float))
                self._range_label = ui.Label("", word_wrap=True)
        self._build_contour_section()

    def _build_contour_section(self):
        post = self._session.post_settings
        with ui.CollapsableFrame("Contours / Streamlines"):
            with ui.VStack(spacing=4, height=0):
                with ui.HStack(height=22):
                    ui.Label("Levels", width=LABEL_W, tooltip="Number of contour levels (band edges / iso-lines)")
                    self._levels = ui.IntDrag(min=1, max=50)
                    self._levels.model.set_value(int(post["contours"]))
                    self._levels.model.add_end_edit_fn(lambda m: self._on_levels(m.as_int))
                with ui.HStack(height=22):
                    ui.Label("Banded colors", width=LABEL_W)
                    self._banded = ui.CheckBox()
                    self._banded.model.add_value_changed_fn(lambda m: self._apply_bands())
                with ui.HStack(height=22):
                    ui.Label("Iso-lines", width=LABEL_W, tooltip="On the surface mesh (needs a mesh)")
                    lines = ui.CheckBox()
                    lines.model.set_value(bool(post["isolines"]))
                    lines.model.add_value_changed_fn(lambda m: self._post(isolines=m.as_bool))
                with ui.HStack(height=22):
                    ui.Label("Isosurfaces", width=LABEL_W,
                             tooltip="Number of isosurfaces (0 = off). Set Show to 'none' to see inside.")
                    iso = ui.IntDrag(min=0, max=20)
                    iso.model.set_value(int(post["isosurfaces"]))
                    iso.model.add_end_edit_fn(lambda m: self._post(isosurfaces=m.as_int))
                with ui.HStack(height=22):
                    ui.Label("Streamlines", width=LABEL_W, tooltip="Seed count, vector fields only (0 = off)")
                    stream = ui.IntDrag(min=0, max=2000, step=10)
                    stream.model.set_value(int(post["streamlines"]))
                    stream.model.add_end_edit_fn(lambda m: self._post(streamlines=m.as_int))
                with ui.HStack(height=22):
                    ui.Label("Seed from", width=LABEL_W, tooltip="Anywhere, or a named selection such as an inlet")
                    self._seed_frame = ui.Frame(build_fn=self._build_seed_combo)
                with ui.HStack(height=22):
                    ui.Label("Tracing", width=LABEL_W,
                             tooltip="fast: fixed one-cell RK4 steps (default); accurate: adaptive RK45, ~6x slower")
                    qualities = ["fast", "accurate"]
                    quality = ui.ComboBox(qualities.index(post.get("stream_quality", "fast")), *qualities)
                    quality.model.get_item_value_model().add_value_changed_fn(
                        lambda m: self._post(stream_quality=qualities[m.as_int]))

    def _build_seed_combo(self):
        info = self._session.model_info
        names = ["anywhere"] + (sorted({ns for r in info["tbroms"] for ns in r["named_selections"]}) if info else [])
        current = self._session.post_settings.get("seed")
        combo = ui.ComboBox(names.index(current) if current in names else 0, *names)
        combo.model.get_item_value_model().add_value_changed_fn(
            lambda m: self._post(seed=None if m.as_int == 0 else names[m.as_int]))

    def _on_levels(self, n):
        self._apply_bands()
        self._post(contours=max(int(n), 1))

    def _apply_bands(self):
        # n levels split the range into n + 1 bands, matching the worker's iso-lines.
        bands = self._levels.model.as_int + 1 if self._banded.model.as_bool else 0
        self._session.updater.restyle(bands=bands)

    def _post(self, **settings):
        self._run(self._session.set_post(**settings), queue=True)

    def _build_log_section(self):
        with ui.CollapsableFrame("Worker log", collapsed=True):
            with ui.ScrollingFrame(height=160):
                self._log_label = ui.Label("", word_wrap=True, alignment=ui.Alignment.LEFT_TOP,
                                           style={"font_size": 12})

    def _refresh_log(self):
        worker = self._session.worker
        if worker.log_version == self._log_version:
            return
        self._log_version = worker.log_version
        marks = {"error": "E", "warning": "W", "info": " "}
        lines = [f"{ts} {marks.get(level, ' ')} {text}" for ts, level, text in list(worker.log)[-60:]]
        self._log_label.text = "\n".join(lines)

    # ---- actions ----------------------------------------------------------------

    def _run(self, coro, queue=False):
        """Run an action; ``queue`` = run even while another action is busy (quick sends)."""
        if self._busy and not queue:
            coro.close()
            return

        async def guarded():
            owns_busy = not queue
            if owns_busy:
                self._busy = True
            try:
                await coro
                self._error_label.text = ""
            except Exception as exc:
                carb.log_error(f"[twinviz.viewer] {exc}")
                self._error_label.text = str(exc)
            finally:
                if owns_busy:
                    self._busy = False
                self._refresh_status()

        asyncio.ensure_future(guarded())

    async def _ping(self):
        latency = await self._session.ping()
        self._error_label.text = f"pong in {latency * 1000:.2f} ms"

    def _refresh_models(self):
        self._model_files = sorted(self._models_dir.glob("*.twin"))
        self._model_combo_frame.rebuild()

    def _on_load(self):
        if not self._model_files:
            self._error_label.text = f"no .twin files in {self._models_dir}"
            return
        idx = self._model_combo.model.get_item_value_model().as_int
        self._model_label.text = f"loading {self._model_files[idx].name}..."
        self._run(self._session.load(self._model_files[idx]))

    def _restyle(self, **style):
        if style.get("vmin") is not None or style.get("vmax") is not None:
            style["auto_range"] = False
        updater = self._session.updater
        updater.restyle(**style)
        if {"auto_range", "vmin", "vmax"} & set(style):
            # Contour levels follow the color range.
            s = updater.style
            self._post(range=None if s["auto_range"] else [s["vmin"], s["vmax"]])

    def _frame_prims(self, paths):
        try:
            from omni.kit.viewport.utility import frame_viewport_prims

            frame_viewport_prims(prims=paths)
        except Exception as exc:
            carb.log_warn(f"[twinviz.viewer] could not frame prims: {exc}")

    # ---- session updates --------------------------------------------------------

    def _on_session_changed(self, what):
        if what == "model":
            info = self._session.model_info
            def describe(r):
                if r.get("decimation"):
                    return f"{r['name']} ({r['shown']:,} of {r['points']:,} points)"
                return r["name"]

            roms = ", ".join(describe(r) for r in info["tbroms"])
            self._model_label.text = f"{info['name']}  mode={info['mode']}  TBROM: {roms}"
            self._inputs_frame.rebuild()
            self._outputs_frame.rebuild()
            self._seed_frame.rebuild()
        elif what == "mesh":
            for rom, st in self._session.mesh_stats.items():
                self._model_label.text += (f"\nmesh {Path(st['path']).name}: {st['triangles']} tris, "
                                           f"{st['unmapped']} unmapped vertices")
        elif what == "error":
            self._error_label.text = self._session.last_error
        elif what == "frame":
            now = time.perf_counter()
            if now - self._last_ui_refresh < UI_REFRESH_S:
                return
            self._last_ui_refresh = now
            self._refresh_frame()
        self._refresh_status()

    def _refresh_status(self):
        s = self._session
        if not s.connected:
            self._status_label.text = "stopped"
        else:
            state = "running" if s.running else "idle"
            self._status_label.text = f"connected (pid {s.client.hello['pid']}), {state}"

    def _refresh_frame(self):
        s = self._session
        f = s.last_frame
        if f is None:
            return
        self._frame_label.text = f"t={f['t']:.3f}  step={f['step']}  {s.fps:.1f} frames/s"
        for name, label in self._output_labels.items():
            v = f["scalars"].get(name)
            label.text = "-" if v is None else f"{v:.6g}"
        ranges = [f"{v.rom}: [{v.range[0]:.4g}, {v.range[1]:.4g}]{' |vec|' if v.is_vector else ''}"
                  for v in s.updater.views.values()]
        self._range_label.text = "\n".join(ranges)
