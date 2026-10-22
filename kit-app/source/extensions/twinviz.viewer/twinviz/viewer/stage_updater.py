"""Mirrors the latest worker geometry/frame into USD on Kit's update tick."""

import re

import carb
import carb.eventdispatcher
import numpy as np
import omni.kit.app
import omni.usd
from pxr import Gf, Sdf, UsdGeom, Vt

from . import colormap

ROOT_PATH = "/World/TwinViz"


def _prim_name(name):
    name = re.sub(r"[^A-Za-z0-9_]", "_", name)
    return name if name[:1].isalpha() or name[:1] == "_" else f"_{name}"


class FieldView:
    """One ``UsdGeom.Points`` prim per TBROM."""

    def __init__(self, stage, rom, points):
        self.rom = rom
        self.base_points = np.ascontiguousarray(points, dtype=np.float32)
        self.path = Sdf.Path(f"{ROOT_PATH}/{_prim_name(rom)}")
        self.range = (0.0, 0.0)
        self.is_vector = False

        geom = UsdGeom.Points.Define(stage, self.path)
        geom.CreatePointsAttr().Set(Vt.Vec3fArray.FromNumpy(self.base_points))
        lo, hi = self.base_points.min(axis=0), self.base_points.max(axis=0)
        geom.CreateExtentAttr().Set(Vt.Vec3fArray([Gf.Vec3f(*map(float, lo)), Gf.Vec3f(*map(float, hi))]))
        diag = float(np.linalg.norm(hi - lo)) or 1.0
        width = diag / max(len(self.base_points), 1) ** (1.0 / 3.0) * 0.8
        geom.CreateWidthsAttr().Set(Vt.FloatArray([width]))
        geom.SetWidthsInterpolation(UsdGeom.Tokens.constant)
        color = geom.CreateDisplayColorPrimvar(UsdGeom.Tokens.vertex)
        color.Set(Vt.Vec3fArray.FromNumpy(np.full_like(self.base_points, 0.5)))
        self._geom = geom
        self._points_attr = geom.GetPointsAttr()
        self._color_attr = color.GetAttr()
        self._deformed = False

    def apply(self, values, style):
        """Write colors (and displaced points for vector fields) for one frame."""
        values = np.asarray(values, dtype=np.float32)
        self.is_vector = values.ndim == 2 and values.shape[1] == 3
        scalars = colormap.scalarize(values)
        if scalars.shape[0] != self.base_points.shape[0]:
            carb.log_warn(f"[twinviz.viewer] {self.rom}: {scalars.shape[0]} values for "
                          f"{self.base_points.shape[0]} points; skipping frame")
            return
        if style["auto_range"]:
            self.range = (float(np.nanmin(scalars)), float(np.nanmax(scalars)))
        else:
            self.range = (style["vmin"], style["vmax"])
        colors = colormap.apply(scalars, *self.range, style["colormap"])

        scale = style["deform_scale"] if self.is_vector else 0.0
        with Sdf.ChangeBlock():
            self._color_attr.Set(Vt.Vec3fArray.FromNumpy(colors))
            if scale:
                pts = self.base_points + values * np.float32(scale)
                self._points_attr.Set(Vt.Vec3fArray.FromNumpy(np.ascontiguousarray(pts)))
                self._deformed = True
            elif self._deformed:
                self._points_attr.Set(Vt.Vec3fArray.FromNumpy(self.base_points))
                self._deformed = False


class StageUpdater:
    def __init__(self, units_in_meters=1.0):
        self.units_in_meters = units_in_meters
        self.style = {
            "colormap": "turbo",
            "auto_range": True,
            "vmin": 0.0,
            "vmax": 1.0,
            "deform_scale": 0.0,
        }
        self.views = {}
        self.frames_applied = 0
        self.on_geometry_built = None  # callback(list of prim paths)
        self._pending_geometry = None
        self._pending_frame = None
        self._last_frame = None
        self._sub = carb.eventdispatcher.get_eventdispatcher().observe_event(
            event_name=omni.kit.app.GLOBAL_EVENT_UPDATE,
            observer_name="twinviz.viewer.StageUpdater",
            on_event=lambda _: self._on_update(),
        )

    def destroy(self):
        self._sub = None
        self.views.clear()

    # Called by the client on the main loop; applied on the next update tick.
    def set_geometry(self, arrays):
        self._pending_geometry = arrays
        self._pending_frame = None

    def set_frame(self, header, arrays):
        self._pending_frame = (header, arrays)

    def restyle(self, **style):
        """Change colormap/range/deformation and re-apply the last frame."""
        self.style.update(style)
        if self._pending_frame is None and self._last_frame is not None:
            self._pending_frame = self._last_frame

    def clear(self):
        self._pending_geometry = None
        self._pending_frame = None
        self._last_frame = None
        self._remove_prims()

    def _remove_prims(self):
        self.views.clear()
        stage = omni.usd.get_context().get_stage()
        if stage and stage.GetPrimAtPath(ROOT_PATH):
            stage.RemovePrim(ROOT_PATH)

    def _on_update(self):
        if self._pending_geometry is None and self._pending_frame is None:
            return
        try:
            if self._pending_geometry is not None:
                arrays, self._pending_geometry = self._pending_geometry, None
                self._build(arrays)
            if self._pending_frame is not None and self.views:
                frame, self._pending_frame = self._pending_frame, None
                self._last_frame = frame
                _, arrays = frame
                for rom, values in arrays.items():
                    view = self.views.get(rom)
                    if view is not None:
                        view.apply(values, self.style)
                self.frames_applied += 1
        except Exception as exc:
            carb.log_error(f"[twinviz.viewer] stage update failed: {exc}")
            self._pending_frame = None

    def _build(self, arrays):
        stage = omni.usd.get_context().get_stage()
        if stage is None:
            return
        self._remove_prims()
        self._last_frame = None
        root = UsdGeom.Xform.Define(stage, ROOT_PATH)
        # Ansys results are SI; convert into the stage's units.
        scale = self.units_in_meters / UsdGeom.GetStageMetersPerUnit(stage)
        root.ClearXformOpOrder()
        root.AddScaleOp().Set(Gf.Vec3f(scale, scale, scale))
        for rom, points in arrays.items():
            self.views[rom] = FieldView(stage, rom, points)
        if self.on_geometry_built is not None:
            self.on_geometry_built([str(v.path) for v in self.views.values()])
