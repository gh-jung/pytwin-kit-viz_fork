"""Mirrors the latest worker geometry/frame into USD on Kit's update tick.

Prims are authored in USD once per model. Per-frame work is split in two:
``prepare`` (colormap, deformation; pure numpy) runs on a background thread, and
``commit`` writes the resulting arrays on the main thread, either straight into
Fabric through USDRT (default, skips USD change processing) or into USD.
"""

import concurrent.futures
import re

import carb
import carb.eventdispatcher
import numpy as np
import omni.kit.app
import omni.usd
from pxr import Gf, Sdf, UsdGeom, UsdShade, Vt

from . import colormap
from .overlays import Overlays

ROOT_PATH = "/World/TwinViz"
MATERIAL_PATH = f"{ROOT_PATH}/Looks/FieldColor"
UNMAPPED_COLOR = np.array([0.1, 0.1, 0.1], dtype=np.float32)  # linear
# Share of the field color emitted rather than lit: enough to read values on the
# unlit side of the model, low enough to keep the diffuse shading as a shape cue.
EMISSION_GAIN = 0.6
EMISSION_PRIMVAR = "fieldEmission"

DISPLAY_POINTS = "points"
DISPLAY_MESH = "mesh"
DISPLAY_NONE = "none"    # hide the field, e.g. to see isosurfaces inside it

BACKEND_FABRIC = "fabric"
BACKEND_USD = "usd"

ATTR_POINTS = "points"
ATTR_COLOR = "primvars:displayColor"
ATTR_EMISSION = f"primvars:{EMISSION_PRIMVAR}"


def _prim_name(name):
    name = re.sub(r"[^A-Za-z0-9_]", "_", name)
    return name if name[:1].isalpha() or name[:1] == "_" else f"_{name}"


def _define_field_material(stage):
    """UsdPreviewSurface: diffuse from ``displayColor``, emission from a scaled copy.

    Lighting alone shades the colormap (dark sides, washed-out tops), so values can't
    be read off the colors; the emission term keeps them readable everywhere.
    """
    material = UsdShade.Material.Define(stage, MATERIAL_PATH)

    def primvar_reader(name, varname):
        reader = UsdShade.Shader.Define(stage, f"{MATERIAL_PATH}/{name}")
        reader.CreateIdAttr("UsdPrimvarReader_float3")
        reader.CreateInput("varname", Sdf.ValueTypeNames.Token).Set(varname)
        return reader.CreateOutput("result", Sdf.ValueTypeNames.Float3)

    surface = UsdShade.Shader.Define(stage, f"{MATERIAL_PATH}/Surface")
    surface.CreateIdAttr("UsdPreviewSurface")
    surface.CreateInput("diffuseColor", Sdf.ValueTypeNames.Color3f).ConnectToSource(
        primvar_reader("DisplayColor", "displayColor"))
    surface.CreateInput("emissiveColor", Sdf.ValueTypeNames.Color3f).ConnectToSource(
        primvar_reader("Emission", EMISSION_PRIMVAR))
    surface.CreateInput("roughness", Sdf.ValueTypeNames.Float).Set(0.9)
    surface.CreateInput("metallic", Sdf.ValueTypeNames.Float).Set(0.0)
    material.CreateSurfaceOutput().ConnectToSource(surface.ConnectableAPI(), "surface")
    return material


def _bind(prim, material):
    UsdShade.MaterialBindingAPI.Apply(prim).Bind(material)


def _extent(points):
    lo, hi = points.min(axis=0), points.max(axis=0)
    return Vt.Vec3fArray([Gf.Vec3f(*map(float, lo)), Gf.Vec3f(*map(float, hi))])


class _ArrayWriter:
    """Writes Vec3f arrays to one prim through USDRT/Fabric or plain USD."""

    def __init__(self, prim, backend):
        self.prim = prim
        self.backend = backend
        self._attrs = {}
        self._rt_prim = None

    def set(self, name, array):
        array = np.ascontiguousarray(array, dtype=np.float32)
        if self.backend == BACKEND_FABRIC:
            try:
                self._fabric_attr(name).Set(_usdrt().Vt.Vec3fArray(array))
                return
            except Exception as exc:
                carb.log_warn(f"[twinviz.viewer] Fabric write failed ({exc}); falling back to USD")
                self.backend = BACKEND_USD
                self._attrs.clear()
        attr = self._attrs.get(name)
        if attr is None:
            attr = self._attrs[name] = self.prim.GetAttribute(name)
        attr.Set(Vt.Vec3fArray.FromNumpy(array))

    def get(self, name):
        """Current value as numpy, read from whichever backend is written to."""
        if self.backend == BACKEND_FABRIC:
            return np.array(self._fabric_attr(name).Get(), dtype=np.float32)
        return np.array(self.prim.GetAttribute(name).Get(), dtype=np.float32)

    def _fabric_attr(self, name):
        attr = self._attrs.get(name)
        if attr is None:
            if self._rt_prim is None:
                rt_stage = _usdrt().Usd.Stage.Attach(omni.usd.get_context().get_stage_id())
                self._rt_prim = rt_stage.GetPrimAtPath(str(self.prim.GetPath()))
            attr = self._attrs[name] = self._rt_prim.GetAttribute(name)
        return attr


def _usdrt():
    import usdrt

    return usdrt


class _Layer:
    """A colored point set backed by one prim: the TBROM points or a mapped mesh surface.

    ``index`` maps each prim vertex to a TBROM point (-1 = unmapped); ``None`` means
    the vertices are the TBROM points themselves.
    """

    def __init__(self, geom, base_points, backend, index=None):
        self.geom = geom
        self.base_points = np.ascontiguousarray(base_points, dtype=np.float32)
        self.index = index
        geom.CreateDisplayColorPrimvar(UsdGeom.Tokens.vertex).Set(
            Vt.Vec3fArray.FromNumpy(np.full_like(self.base_points, 0.5)))
        UsdGeom.PrimvarsAPI(geom).CreatePrimvar(
            EMISSION_PRIMVAR, Sdf.ValueTypeNames.Color3fArray, UsdGeom.Tokens.vertex
        ).Set(Vt.Vec3fArray.FromNumpy(np.zeros_like(self.base_points)))
        self.writer = _ArrayWriter(geom.GetPrim(), backend)
        self._deformed = False
        if index is not None:
            self._valid = index >= 0
            self._safe_index = np.where(self._valid, index, 0)

    def set_visible(self, visible):
        img = UsdGeom.Imageable(self.geom)
        img.MakeVisible() if visible else img.MakeInvisible()

    def prepare(self, values, scalars, vrange, cmap, deform_scale, bands=0):
        """Arrays to write for one frame. Pure numpy: safe off the main thread."""
        if self.index is not None:
            values = values[self._safe_index] if deform_scale else None
            scalars = scalars[self._safe_index]
        colors = colormap.apply(scalars, *vrange, cmap, bands=bands)
        if self.index is not None:
            colors[~self._valid] = UNMAPPED_COLOR
        out = {ATTR_COLOR: colors, ATTR_EMISSION: colors * np.float32(EMISSION_GAIN)}
        if deform_scale:
            disp = values * np.float32(deform_scale)
            if self.index is not None:
                disp[~self._valid] = 0.0
            out[ATTR_POINTS] = self.base_points + disp
        return out

    def commit(self, arrays):
        for name in (ATTR_COLOR, ATTR_EMISSION):
            self.writer.set(name, arrays[name])
        if ATTR_POINTS in arrays:
            self.writer.set(ATTR_POINTS, arrays[ATTR_POINTS])
            self._deformed = True
        elif self._deformed:
            self.writer.set(ATTR_POINTS, self.base_points)
            self._deformed = False


class FieldView:
    """Prims for one TBROM: a ``UsdGeom.Points`` cloud and/or a mapped mesh.

    Each prim is authored only when it is first shown: for a large model with a
    mesh, building the unused point cloud doubled the load hitch.
    """

    def __init__(self, stage, rom, points, backend):
        self.rom = rom
        self.backend = backend
        self.n_points = len(points)
        self.path = Sdf.Path(f"{ROOT_PATH}/{_prim_name(rom)}")
        self.mesh_path = None
        self.range = (0.0, 0.0)
        self.is_vector = False
        self._stage = stage
        self._base_points = np.ascontiguousarray(points, dtype=np.float32)
        span = self._base_points.max(axis=0) - self._base_points.min(axis=0) if len(points) else np.ones(3)
        self.diag = float(np.linalg.norm(span)) or 1.0
        self.points_layer = None
        self.mesh_layer = None

    def _create_points_layer(self):
        stage = self._stage
        geom = UsdGeom.Points.Define(stage, self.path)
        base = self._base_points
        geom.CreatePointsAttr().Set(Vt.Vec3fArray.FromNumpy(base))
        geom.CreateExtentAttr().Set(_extent(base))
        diag = float(np.linalg.norm(base.max(axis=0) - base.min(axis=0))) or 1.0
        width = diag / max(len(base), 1) ** (1.0 / 3.0) * 0.8
        geom.CreateWidthsAttr().Set(Vt.FloatArray([width]))
        geom.SetWidthsInterpolation(UsdGeom.Tokens.constant)
        _bind(geom.GetPrim(), UsdShade.Material.Get(stage, MATERIAL_PATH))
        self.points_layer = _Layer(geom, base, self.backend)

    @property
    def base_points(self):
        return self._base_points

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
        _bind(mesh.GetPrim(), UsdShade.Material.Get(stage, MATERIAL_PATH))
        self.mesh_layer = _Layer(mesh, points, self.backend, np.asarray(index, dtype=np.int64))

    def active_layer(self, display):
        if display == DISPLAY_NONE:
            return None
        use_mesh = display == DISPLAY_MESH and self.mesh_layer is not None
        return self.mesh_layer if use_mesh else self.points_layer

    def ensure_layer(self, display):
        """Author the prim ``display`` needs (main thread only)."""
        if display != DISPLAY_NONE and self.active_layer(display) is None:
            self._create_points_layer()

    def set_display(self, display):
        active = self.active_layer(display)
        for layer in (self.points_layer, self.mesh_layer):
            if layer is not None:
                layer.set_visible(layer is active)
        return active

    def prepare(self, values, style):
        """Colormap/deformation for one frame (background thread). Returns None to skip."""
        values = np.asarray(values, dtype=np.float32)
        is_vector = values.ndim == 2 and values.shape[1] == 3
        scalars = colormap.scalarize(values)
        if scalars.shape[0] != self.n_points:
            carb.log_warn(f"[twinviz.viewer] {self.rom}: {scalars.shape[0]} values for "
                          f"{self.n_points} points; skipping frame")
            return None
        if style["auto_range"]:
            vrange = (float(np.nanmin(scalars)), float(np.nanmax(scalars)))
        else:
            vrange = (style["vmin"], style["vmax"])
        deform = style["deform_scale"] if is_vector else 0.0
        # Only the visible representation is updated; restyle() re-applies on switch.
        layer = self.active_layer(style["display"])
        if layer is None:
            return None, None, vrange, is_vector
        arrays = layer.prepare(values, scalars, vrange, style["colormap"], deform, style.get("bands", 0))
        return layer, arrays, vrange, is_vector

    def commit(self, prepared, display):
        layer, arrays, self.range, self.is_vector = prepared
        self.set_display(display)
        if layer is not None:
            layer.commit(arrays)

    def apply(self, values, style):
        """prepare + commit on the calling thread."""
        prepared = self.prepare(values, style)
        if prepared is not None:
            self.commit(prepared, style["display"])


class StageUpdater:
    def __init__(self, units_in_meters=1.0, backend=BACKEND_FABRIC):
        self.units_in_meters = units_in_meters
        self.backend = backend
        self.style = {
            "colormap": "turbo",
            "auto_range": True,
            "vmin": 0.0,
            "vmax": 1.0,
            "deform_scale": 0.0,
            "display": DISPLAY_MESH,  # falls back to points when no mesh is attached
            "bands": 0,               # 0 = smooth colors, n = n flat bands
        }
        self.overlays = Overlays(MATERIAL_PATH, EMISSION_PRIMVAR, EMISSION_GAIN)
        self.posts_applied = 0
        self._pending_post = None
        self.views = {}
        self.frames_applied = 0
        self.on_geometry_built = None  # callback(list of prim paths)
        self._pending_geometry = None
        self._pending_meshes = None
        self._pending_frame = None
        self._last_frame = None
        self._executor = concurrent.futures.ThreadPoolExecutor(1, thread_name_prefix="twinviz-prepare")
        self._job = None          # (future, generation, display, style_version)
        self.style_version = 0          # bumped by restyle()
        self.applied_style_version = 0  # style of the last committed frame
        self._generation = 0      # bumped when prims are rebuilt; stale jobs are dropped
        self._frame_on_commit = False
        self._sub = carb.eventdispatcher.get_eventdispatcher().observe_event(
            event_name=omni.kit.app.GLOBAL_EVENT_UPDATE,
            observer_name="twinviz.viewer.StageUpdater",
            on_event=lambda _: self._on_update(),
        )

    def destroy(self):
        self._sub = None
        self._job = None
        self._executor.shutdown(wait=False, cancel_futures=True)
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

    def set_post(self, header, arrays):
        """Derived geometry (``post`` message); applied on the next update tick."""
        self._pending_post = (header, arrays)

    def restyle(self, **style):
        """Change colormap/range/deformation/display and re-apply the last frame."""
        self.style.update(style)
        self.style_version += 1
        if self._pending_frame is None and self._last_frame is not None:
            self._pending_frame = self._last_frame

    def clear(self):
        self._pending_geometry = None
        self._pending_post = None
        self._pending_meshes = None
        self._pending_frame = None
        self._last_frame = None
        self._remove_prims()

    def _remove_prims(self):
        self._generation += 1
        self.views.clear()
        self.overlays.clear()
        stage = omni.usd.get_context().get_stage()
        if stage and stage.GetPrimAtPath(ROOT_PATH):
            stage.RemovePrim(ROOT_PATH)

    @property
    def settled(self):
        """Nothing queued or in flight, and the latest style is on screen."""
        return (self._pending_frame is None and self._job is None
                and self.applied_style_version == self.style_version)

    def read_attr(self, rom, name, mesh=False):
        """Read back a per-vertex array as rendered (Fabric or USD); for tests and tools."""
        view = self.views[rom]
        layer = view.mesh_layer if mesh else view.points_layer
        if layer is None:
            raise LookupError(f"{rom}: {'mesh' if mesh else 'points'} prim not created (never shown)")
        return layer.writer.get(name)

    def _on_update(self):
        if (self._pending_geometry is None and self._pending_meshes is None
                and self._pending_frame is None and self._job is None and self._pending_post is None):
            return
        try:
            if self._pending_geometry is not None:
                arrays, self._pending_geometry = self._pending_geometry, None
                self._build(arrays)
            if self._pending_meshes is not None and self.views:
                arrays, self._pending_meshes = self._pending_meshes, None
                self._attach_meshes(arrays)
            self._commit_finished_job()
            if self._pending_post is not None and self.views:
                (header, arrays), self._pending_post = self._pending_post, None
                self.overlays.apply(omni.usd.get_context().get_stage(), self.views, header, arrays, self.style)
                self.posts_applied += 1
            if self._pending_frame is not None and self.views and self._job is None:
                frame, self._pending_frame = self._pending_frame, None
                self._last_frame = frame
                self._submit(frame)
        except Exception as exc:
            carb.log_error(f"[twinviz.viewer] stage update failed: {exc}")
            self._pending_frame = None
            self._job = None

    def _submit(self, frame):
        _, arrays = frame
        style = dict(self.style)
        views = dict(self.views)
        for view in views.values():
            view.ensure_layer(style["display"])

        def prepare():
            return {rom: views[rom].prepare(values, style)
                    for rom, values in arrays.items() if rom in views}

        self._job = (self._executor.submit(prepare), self._generation, style["display"], self.style_version)

    def _commit_finished_job(self):
        if self._job is None or not self._job[0].done():
            return
        future, generation, display, style_version = self._job
        self._job = None
        if generation != self._generation:
            return  # prims were rebuilt while preparing
        results = future.result()
        with Sdf.ChangeBlock():
            for rom, prepared in results.items():
                if prepared is not None:
                    self.views[rom].commit(prepared, display)
        self.frames_applied += 1
        self.applied_style_version = style_version
        if self._frame_on_commit and self.on_geometry_built is not None:
            # Prims exist only after the first commit (they are created on demand).
            self._frame_on_commit = False
            self.on_geometry_built([ROOT_PATH])

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
        _define_field_material(stage)
        for rom, points in arrays.items():
            self.views[rom] = FieldView(stage, rom, points, self.backend)
        self._frame_on_commit = True

    def _attach_meshes(self, arrays):
        stage = omni.usd.get_context().get_stage()
        for rom, view in self.views.items():
            if f"{rom}/points" in arrays:
                view.attach_mesh(stage, arrays[f"{rom}/points"], arrays[f"{rom}/faces"],
                                 arrays[f"{rom}/index"])
                view.set_display(self.style["display"])
