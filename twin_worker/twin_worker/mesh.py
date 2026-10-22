"""Load a source mesh and map its surface vertices onto TBROM points.

Kit renders the triangulated outer surface and colors each vertex with the
value of the nearest TBROM point (``index``). For nodal TBROMs the nearest point
is the same node; for elemental ones it is the nearest element centroid.
Vertices with no TBROM point within the tolerance (e.g. bodies outside the ROM's
named selection) get index -1 and are drawn gray. The tolerance is a multiple of the
median TBROM point spacing; element centroids sit about half an element inside the
surface, so elemental data needs a larger multiple than nodal data.
"""

import json
import logging
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

SIDECAR_SUFFIX = ".twinviz.json"
# Tolerance in multiples of the median nearest-neighbour spacing of TBROM points.
TOLERANCE_FACTOR = {"nodal": 1.5, "elemental": 4.0}


def read_sidecar(twin_path):
    """``<model>.twinviz.json`` next to the twin -> ``({rom or "*": mesh path}, tolerance_factor)``.

    Format::

        {"meshes": {"<rom name>": "mesh.rst"}}   # per TBROM
        {"mesh": "mesh.rst"}                      # same mesh for every TBROM
        {"mesh": "...", "tolerance_factor": 6}   # optional, overrides TOLERANCE_FACTOR
    """
    twin_path = Path(twin_path)
    sidecar = twin_path.with_name(twin_path.stem + SIDECAR_SUFFIX)
    if not sidecar.is_file():
        return {}, None
    cfg = json.loads(sidecar.read_text(encoding="utf-8"))
    meshes = dict(cfg.get("meshes", {}))
    if "mesh" in cfg:
        meshes.setdefault("*", cfg["mesh"])
    paths = {rom: str((sidecar.parent / p).resolve()) for rom, p in meshes.items()}
    return paths, cfg.get("tolerance_factor")


def load_mesh(path):
    """Read a mesh file into a pyvista dataset."""
    path = Path(path)
    ext = path.suffix.lower()
    if ext == ".rst":
        from ansys.mapdl import reader

        return reader.read_binary(str(path)).grid
    if ext in (".cdb", ".dat"):
        from .cdb import read_cdb, to_pyvista

        return to_pyvista(*read_cdb(path))
    import pyvista as pv

    return pv.read(str(path))


def surface_mapping(mesh, tbrom_points, tolerance_factor=TOLERANCE_FACTOR["nodal"]):
    """Return ``(points f32 (V,3), faces i32 (F,3), index i32 (V,), stats)``."""
    from scipy.spatial import cKDTree

    surf = mesh.extract_surface(algorithm="dataset_surface").triangulate()
    # Solver meshes don't guarantee outward winding; inward faces render dark in RTX.
    surf = surf.compute_normals(cell_normals=False, point_normals=True, split_vertices=False,
                                consistent_normals=True, auto_orient_normals=True)
    points = np.asarray(surf.points, dtype=np.float32)
    faces = np.asarray(surf.faces).reshape(-1, 4)[:, 1:].astype(np.int32)

    tbrom_points = np.asarray(tbrom_points, dtype=np.float64)
    tree = cKDTree(tbrom_points)
    if len(tbrom_points) > 1:
        spacing, _ = tree.query(tbrom_points, k=2)
        tolerance = tolerance_factor * float(np.median(spacing[:, 1]))
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


def build_meshes(mesh_paths, tbrom_points, locations=None, tolerance_factor=None):
    """``{rom: mesh path}`` (``"*"`` = all) and ``{rom: points}`` -> arrays and stats per ROM.

    ``locations`` maps ROM -> TBROM data location ("Nodal"/"elemental") to pick the
    default tolerance; ``tolerance_factor`` overrides it for every ROM.
    """
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
        factor = tolerance_factor
        if factor is None:
            location = str((locations or {}).get(rom, "nodal")).lower()
            factor = TOLERANCE_FACTOR.get(location, TOLERANCE_FACTOR["nodal"])
        points, faces, index, st = surface_mapping(cache[path], pts, factor)
        arrays[f"{rom}/points"] = points
        arrays[f"{rom}/faces"] = faces
        arrays[f"{rom}/index"] = index
        stats[rom] = {"path": path, **st}
        log.info("mesh for %s: %s", rom, stats[rom])
    return arrays, stats
