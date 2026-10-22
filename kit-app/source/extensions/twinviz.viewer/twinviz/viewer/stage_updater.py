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
UNMAPPED_COLOR = np.array([0.35, 0.35, 0.35], dtype=np.float32)

DISPLAY_POINTS = "points"
DISPLAY_MESH = "mesh"


def _prim_name(name):
    name = re.sub(r"[^A-Za-z0-9_]", "_", name)
    return name if name[:1].isalpha() or name[:1] == "_" else f"_{name}"


def _extent(points):
    lo, hi = points.min(axis=0), points.max(axis=0)
    return Vt.Vec3fArray([Gf.Vec3f(*map(float, lo)), Gf.Vec3f(*map(float, hi))])


class _Layer:
    """A colored point set backed by one prim: the TBROM points or a mapped mesh surface.

    ``index`` maps each prim vertex to a TBROM point (-1 = unmapped); ``None`` means
    the vertices are the TBROM points themselves.
    """

    def __init__(self, geom, base_points, index=None):
        self.geom = geom
        self.base_points = np.ascontiguousarray(base_points, dtype=np.float32)
        self.index = index
        self._points_attr = geom.GetPointsAttr()
        self._color_attr = geom.CreateDisplayColorPrimvar(UsdGeom.Tokens.vertex).GetAttr()
        self._color_attr.Set(Vt.Vec3fArray.FromNumpy(np.full_like(self.base_points, 0.5)))
        self._deformed = False
        if index is not None:
            self._valid = index >= 0
            self._safe_index = np.where(self._valid, index, 0)

    def set_visible(self, visible):
        img = UsdGeom.Imageable(self.geom)
        img.MakeVisible() if visible else img.MakeInvisible()

    def apply(self, values, scalars, vrange, style, deform_scale):
        if self.index is not None:
            values = values[self._safe_index]
            scalars = scalars[self._safe_index]
        colors = colormap.apply(scalars, *vrange, style["colormap"])
        if self.index is not None:
            colors[~self._valid] = UNMAPPED_COLOR
        with Sdf.ChangeBlock():
            self._color_attr.Set(Vt.Vec3fArray.FromNumpy(colors))
            if deform_scale:
                disp = values * np.float32(deform_scale)
                if self.index is not None:
                    disp[~self._valid] = 0.0
                pts = np.ascontiguousarray(self.base_points + disp)
                self._points_attr.Set(Vt.Vec3fArray.FromNumpy(pts))
                self._deformed = True
            elif self._deformed:
                self._points_attr.Set(Vt.Vec3fArray.FromNumpy(self.base_points))
                self._deformed = False


class FieldView:
    """Prims for one TBROM: always a ``UsdGeom.Points`` cloud, optionally a mapped mesh."""

    def __init__(self, stage, rom, points):
        self.rom = rom
        self.n_points = len(points)
        self.path = Sdf.Path(f"{ROOT_PATH}/{_prim_name(rom)}")
        self.mesh_path = None
        self.range = (0.0, 0.0)
        self.is_vector = False

        geom = UsdGeom.Points.Define(stage, self.path)
        base = np.ascontiguousarray(points, dtype=np.float32)
        geom.CreatePointsAttr().Set(Vt.Vec3fArray.FromNumpy(base))
        geom.CreateExtentAttr().Set(_extent(base))
        diag = float(np.linalg.norm(base.max(axis=0) - base.min(axis=0))) or 1.0
        width = diag / max(len(base), 1) ** (1.0 / 3.0) * 0.8
        geom.CreateWidthsAttr().Set(Vt.FloatArray([width]))
        geom.SetWidthsInterpolation(UsdGeom.Tokens.constant)
        self.points_layer = _Layer(geom, base)
        self.mesh_layer = None

    @property
    def base_points(self):
        return self.points_layer.base_points

    def attach_mesh(self, stage, points, faces, index):
        self.mesh_path = Sdf.Path(f"{self.path}_mesh")
        mesh = UsdGeom.Mesh.Define(stage, self.mesh_path)
        points = np.ascontiguousarray(points, dtype=np.float32)
        mesh.CreatePointsAttr().Set(Vt.Vec3fArray.FromNumpy(points))
        mesh.CreateExtentAttr().Set(_extent(points))
        mesh.CreateFaceVertexCountsAttr().Set(Vt.IntArray.FromNumpy(np.full(len(faces), 3, np.int32)))
        mesh.CreateFaceVertexIndicesAttr().Set(
            Vt.IntArray.FromNumpy(np.ascontiguousarray(faces, dtype=np.int32).ravel()))
        mesh.CreateSubdivisionSchemeAttr().Set(UsdGeom.Tokens.none)
        mesh.CreateDoubleSidedAttr().Set(True)
        self.mesh_layer = _Layer(mesh, points, np.asarray(index, dtype=np.int64))

    def set_display(self, display):
        use_mesh = display == DISPLAY_MESH and self.mesh_layer is not None
        self.points_layer.set_visible(not use_mesh)
        if self.mesh_layer is not None:
            self.mesh_layer.set_visible(use_mesh)
        return self.mesh_layer if use_mesh else self.points_layer

    def apply(self, values, style):
        """Write colors (and displaced points for vector fields) for one frame."""
        values = np.asarray(values, dtype=np.float32)
        self.is_vector = values.ndim == 2 and values.shape[1] == 3
        scalars = colormap.scalarize(values)
        if scalars.shape[0] != self.n_points:
            carb.log_warn(f"[twinviz.viewer] {self.rom}: {scalars.shape[0]} values for "
                          f"{self.n_points} points; skipping frame")
            return
        if style["auto_range"]:
            self.range = (float(np.nanmin(scalars)), float(np.nanmax(scalars)))
        else:
            self.range = (style["vmin"], style["vmax"])
        deform = style["deform_scale"] if self.is_vector else 0.0
        # Only the visible representation is updated; restyle() re-applies on switch.
        layer = self.set_display(style["display"])
        layer.apply(values, scalars, self.range, style, deform)


class StageUpdater:
    def __init__(self, units_in_meters=1.0):
        self.units_in_meters = units_in_meters
        self.style = {
            "colormap": "turbo",
            "auto_range": True,
            "vmin": 0.0,
            "vmax": 1.0,
            "deform_scale": 0.0,
            "display": DISPLAY_MESH,  # falls back to points when no mesh is attached
        }
        self.views = {}
        self.frames_applied = 0
        self.on_geometry_built = None  # callback(list of prim paths)
        self._pending_geometry = None
        self._pending_meshes = None
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

    @property
    def has_mesh(self):
        return any(v.mesh_layer is not None for v in self.views.values())

    # Called by the client on the main loop; applied on the next update tick.
    def set_geometry(self, arrays):
        self._pending_geometry = arrays
        self._pending_meshes = None
        self._pending_frame = None

    def set_meshes(self, arrays):
        """``{"<rom>/points", "<rom>/faces", "<rom>/index"}`` arrays from a ``mesh_map`` message."""
        self._pending_meshes = arrays

    def set_frame(self, header, arrays):
        self._pending_frame = (header, arrays)

    def restyle(self, **style):
        """Change colormap/range/deformation/display and re-apply the last frame."""
        self.style.update(style)
        if self._pending_frame is None and self._last_frame is not None:
            self._pending_frame = self._last_frame

    def clear(self):
        self._pending_geometry = None
        self._pending_meshes = None
        self._pending_frame = None
        self._last_frame = None
        self._remove_prims()

    def _remove_prims(self):
        self.views.clear()
        stage = omni.usd.get_context().get_stage()
        if stage and stage.GetPrimAtPath(ROOT_PATH):
            stage.RemovePrim(ROOT_PATH)

    def _on_update(self):
        if self._pending_geometry is None and self._pending_meshes is None and self._pending_frame is None:
            return
        try:
            if self._pending_geometry is not None:
                arrays, self._pending_geometry = self._pending_geometry, None
                self._build(arrays)
            if self._pending_meshes is not None and self.views:
                arrays, self._pending_meshes = self._pending_meshes, None
                self._attach_meshes(arrays)
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

    def _attach_meshes(self, arrays):
        stage = omni.usd.get_context().get_stage()
        for rom, view in self.views.items():
            if f"{rom}/points" in arrays:
                view.attach_mesh(stage, arrays[f"{rom}/points"], arrays[f"{rom}/faces"],
                                 arrays[f"{rom}/index"])
                view.set_display(self.style["display"])
