"""Load a source mesh and map its surface vertices onto TBROM points.

Kit renders the triangulated outer surface and colors each vertex with the
value of the nearest TBROM point (``index``). For nodal TBROMs the nearest point
is the same node; for elemental ones it is the nearest element centroid.
Vertices with no TBROM point within ``tolerance`` (e.g. bodies outside the ROM's
named selection) get index -1 and are drawn gray.
"""

import json
import logging
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

SIDECAR_SUFFIX = ".twinviz.json"
# Tolerance in multiples of the median nearest-neighbour spacing of TBROM points.
TOLERANCE_FACTOR = 1.5


def read_sidecar(twin_path):
    """``<model>.twinviz.json`` next to the twin -> ``{rom_name or "*": mesh_path}``.

    Format::

        {"meshes": {"<rom name>": "mesh.rst"}}   # per TBROM
        {"mesh": "mesh.rst"}                      # same mesh for every TBROM
    """
    twin_path = Path(twin_path)
    sidecar = twin_path.with_name(twin_path.stem + SIDECAR_SUFFIX)
    if not sidecar.is_file():
        return {}
    cfg = json.loads(sidecar.read_text(encoding="utf-8"))
    meshes = dict(cfg.get("meshes", {}))
    if "mesh" in cfg:
        meshes.setdefault("*", cfg["mesh"])
    return {rom: str((sidecar.parent / p).resolve()) for rom, p in meshes.items()}


def load_mesh(path):
    """Read a mesh file into a pyvista dataset."""
    path = Path(path)
    ext = path.suffix.lower()
    if ext == ".rst":
        from ansys.mapdl import reader

        return reader.read_binary(str(path)).grid
    if ext in (".cdb", ".dat"):
        from ansys.mapdl import reader

        return reader.Archive(str(path)).grid
    import pyvista as pv

    return pv.read(str(path))


def surface_mapping(mesh, tbrom_points):
    """Return ``(points f32 (V,3), faces i32 (F,3), index i32 (V,), stats)``."""
    from scipy.spatial import cKDTree

    surf = mesh.extract_surface(algorithm="dataset_surface").triangulate()
    points = np.asarray(surf.points, dtype=np.float32)
    faces = np.asarray(surf.faces).reshape(-1, 4)[:, 1:].astype(np.int32)

    tbrom_points = np.asarray(tbrom_points, dtype=np.float64)
    tree = cKDTree(tbrom_points)
    if len(tbrom_points) > 1:
        spacing, _ = tree.query(tbrom_points, k=2)
        tolerance = TOLERANCE_FACTOR * float(np.median(spacing[:, 1]))
    else:
        tolerance = np.inf
    dist, index = tree.query(points.astype(np.float64))
    index = index.astype(np.int32)
    unmapped = dist > tolerance
    index[unmapped] = -1

    stats = {
        "vertices": int(len(points)),
        "triangles": int(len(faces)),
        "unmapped": int(unmapped.sum()),
        "max_distance": float(dist[~unmapped].max()) if (~unmapped).any() else None,
        "tolerance": tolerance,
    }
    return points, faces, index, stats


def build_meshes(mesh_paths, tbrom_points):
    """``{rom: mesh path}`` (``"*"`` = all) and ``{rom: points}`` -> arrays and stats per ROM."""
    arrays = {}
    stats = {}
    cache = {}
    for rom, pts in tbrom_points.items():
        path = mesh_paths.get(rom) or mesh_paths.get("*")
        if not path:
            continue
        if path not in cache:
            log.info("reading mesh %s", path)
            cache[path] = load_mesh(path)
        points, faces, index, st = surface_mapping(cache[path], pts)
        arrays[f"{rom}/points"] = points
        arrays[f"{rom}/faces"] = faces
        arrays[f"{rom}/index"] = index
        stats[rom] = {"path": path, **st}
        log.info("mesh for %s: %s", rom, stats[rom])
    return arrays, stats
