"""USD prims for the worker's derived geometry: iso-lines, isosurfaces, streamlines.

Their topology changes every update, so they are written through USD (not Fabric).
Each (TBROM, kind) owns one prim next to the field prims; kinds missing from the
newest ``post`` message are removed.
"""

import numpy as np
from pxr import Gf, Sdf, UsdGeom, UsdShade, Vt

from . import colormap

ISOLINES = "isolines"
ISOSURFACES = "isosurfaces"
STREAMLINES = "streamlines"

ISOLINE_COLOR = np.array([0.01, 0.01, 0.01], dtype=np.float32)  # linear, near black
# Curve widths relative to the model size.
WIDTH = {ISOLINES: 0.0025, STREAMLINES: 0.004}


def _fields(arrays, rom, kind):
    prefix = f"{rom}/{kind}/"
    return {k[len(prefix):]: v for k, v in arrays.items() if k.startswith(prefix)}


# 워커의 파생 형상 전용 표시 계층. 기본 points/mesh의 Fabric 프레임 갱신과 구분된다.
class Overlays:
    def __init__(self, material_path, emission_primvar, emission_gain):
        self._material_path = material_path
        self._emission_primvar = emission_primvar
        self._emission_gain = emission_gain
        self.paths = {}  # (rom, kind) -> prim path
        self.last_items = []

    def clear(self):
        self.paths.clear()
        self.last_items = []

    # 이번 post 메시지에 없는 (ROM, 종류)의 prim은 제거해 이전 결과가 남지 않게 한다.
    def apply(self, stage, views, header, arrays, style):
        present = set()
        for item in header.get("items", []):
            rom, kind = item["rom"], item["kind"]
            view = views.get(rom)
            if view is None:
                continue
            data = _fields(arrays, rom, kind)
            path = f"{view.path}_{kind}"
            if kind == ISOSURFACES:
                self._surface(stage, path, data, item, style)
            else:
                self._curves(stage, path, data, kind, view.diag)
            self.paths[(rom, kind)] = path
            present.add((rom, kind))
        for key in [k for k in self.paths if k not in present]:
            prim = stage.GetPrimAtPath(self.paths.pop(key))
            if prim:
                stage.RemovePrim(prim.GetPath())
        self.last_items = header.get("items", [])

    # ---- prim writers ------------------------------------------------------------

    def _colors(self, prim, colors, interpolation):
        primvars = UsdGeom.PrimvarsAPI(prim)
        primvars.CreatePrimvar("displayColor", Sdf.ValueTypeNames.Color3fArray, interpolation).Set(
            Vt.Vec3fArray.FromNumpy(np.ascontiguousarray(colors, dtype=np.float32)))
        primvars.CreatePrimvar(self._emission_primvar, Sdf.ValueTypeNames.Color3fArray, interpolation).Set(
            Vt.Vec3fArray.FromNumpy(np.ascontiguousarray(colors * np.float32(self._emission_gain))))

    def _bind(self, prim, stage):
        material = UsdShade.Material.Get(stage, self._material_path)
        if material:
            UsdShade.MaterialBindingAPI.Apply(prim).Bind(material)

    # 모든 선을 하나의 BasisCurves 배열로 기록한다. counts가 각 선의 points 구간을 정의한다.
    def _curves(self, stage, path, data, kind, diag):
        counts = np.asarray(data.get("counts", np.zeros(0, np.int32)), dtype=np.int32)
        points = np.asarray(data.get("points", np.zeros((0, 3), np.float32)), dtype=np.float32)
        scalars = np.asarray(data.get("scalars", np.zeros(0, np.float32)), dtype=np.float32)
        # Linear curves need at least two vertices each.
        if len(counts) and (counts < 2).any():
            keep = np.repeat(counts >= 2, counts)
            points, scalars, counts = points[keep], scalars[keep], counts[counts >= 2]

        curves = UsdGeom.BasisCurves.Define(stage, path)
        curves.CreateTypeAttr().Set(UsdGeom.Tokens.linear)
        curves.CreatePointsAttr().Set(Vt.Vec3fArray.FromNumpy(points))
        curves.CreateCurveVertexCountsAttr().Set(Vt.IntArray.FromNumpy(counts))
        curves.CreateWidthsAttr().Set(Vt.FloatArray([WIDTH[kind] * diag]))
        curves.SetWidthsInterpolation(UsdGeom.Tokens.constant)
        if len(points):
            lo, hi = points.min(0), points.max(0)
            curves.CreateExtentAttr().Set(Vt.Vec3fArray([Gf.Vec3f(*map(float, lo)), Gf.Vec3f(*map(float, hi))]))
        if kind == ISOLINES:
            self._colors(curves.GetPrim(), ISOLINE_COLOR[None, :], UsdGeom.Tokens.constant)
        else:
            vmin, vmax = (float(scalars.min()), float(scalars.max())) if len(scalars) else (0.0, 1.0)
            self._colors(curves.GetPrim(), colormap.apply(scalars, vmin, vmax), UsdGeom.Tokens.vertex)
        self._bind(curves.GetPrim(), stage)

    # 등치면은 삼각형 Mesh로 만들고 레벨 값을 컬러맵에 대응시킨다.
    def _surface(self, stage, path, data, item, style):
        points = np.asarray(data.get("points", np.zeros((0, 3), np.float32)), dtype=np.float32)
        faces = np.asarray(data.get("faces", np.zeros((0, 3), np.int32)), dtype=np.int32)
        scalars = np.asarray(data.get("scalars", np.zeros(0, np.float32)), dtype=np.float32)

        mesh = UsdGeom.Mesh.Define(stage, path)
        mesh.CreatePointsAttr().Set(Vt.Vec3fArray.FromNumpy(points))
        mesh.CreateFaceVertexCountsAttr().Set(Vt.IntArray.FromNumpy(np.full(len(faces), 3, np.int32)))
        mesh.CreateFaceVertexIndicesAttr().Set(Vt.IntArray.FromNumpy(np.ascontiguousarray(faces).ravel()))
        mesh.CreateSubdivisionSchemeAttr().Set(UsdGeom.Tokens.none)
        mesh.CreateDoubleSidedAttr().Set(True)
        if len(points):
            lo, hi = points.min(0), points.max(0)
            mesh.CreateExtentAttr().Set(Vt.Vec3fArray([Gf.Vec3f(*map(float, lo)), Gf.Vec3f(*map(float, hi))]))
        # Same colors as the field: each isosurface takes its level's color.
        vmin, vmax = item.get("range") or (0.0, 1.0)
        colors = colormap.apply(scalars, vmin, vmax, style["colormap"], bands=style.get("bands", 0))
        self._colors(mesh.GetPrim(), colors, UsdGeom.Tokens.vertex)
        self._bind(mesh.GetPrim(), stage)
