"""Derived geometry from TBROM fields: surface iso-lines, isosurfaces and streamlines.

Each TBROM gets a ``Domain``: the outer surface and the volume on which VTK filters
run, plus index arrays that gather the current field values onto their nodes.

* With a source mesh, the surface is its extracted outer skin and the volume is
  the mesh itself (nodes mapped to the nearest TBROM point).
* Without one, the volume is a voxel grid over the points, filled from the nearest
  TBROM point.

Volume nodes farther than the mapping tolerance from every TBROM point (outside the
data, or bodies the ROM does not cover) are cut away once, together with their
cells, so isosurfaces and streamlines end at the data boundary instead of
inventing values there.

``PostWorker`` runs the filters on its own thread so the time-stepping loop is not
slowed down; it always works on the newest frame and drops older ones.
"""

import logging
import threading
import time

import numpy as np

log = logging.getLogger(__name__)

VOXEL_BUDGET = 96 ** 3      # max grid nodes when no mesh is available
MIN_INTERVAL_S = 0.1        # at most ~10 post updates per second
STREAM_MAX_STEPS = 2000

# VTK cell types with a volume (linear and quadratic tets, hexes, wedges, pyramids, ...).
VOLUME_CELL_TYPES = {10, 11, 12, 13, 14, 15, 16, 24, 25, 26, 27, 29, 32, 33, 42}

ISOLINES = "isolines"
ISOSURFACES = "isosurfaces"
STREAMLINES = "streamlines"


def _scalars(values):
    values = np.asarray(values, dtype=np.float32)
    if values.ndim == 2:
        return np.sqrt(np.einsum("ij,ij->i", values, values))
    return values


def contour_levels(vmin, vmax, count):
    """``count`` levels splitting [vmin, vmax] into equal bands (the band edges Kit draws)."""
    if count <= 0 or not np.isfinite(vmin) or not np.isfinite(vmax) or vmax <= vmin:
        return []
    return list(np.linspace(vmin, vmax, count + 2)[1:-1])


class Domain:
    def __init__(self, points, surface=None, surface_index=None, volume=None, volume_index=None, tolerance=None):
        self.points = np.asarray(points, dtype=np.float64)
        self._tree = None
        self._tolerance = tolerance
        self.surface = surface
        self.surface_index = surface_index
        self.volume = volume
        self.volume_index = volume_index
        self.voxel = volume is None
        self.seed_sets = {}  # named selection -> point coordinates, for seeding streamlines
        self._diag = float(np.linalg.norm(self.points.max(0) - self.points.min(0))) or 1.0

    # Built on first use: a KD-tree over a million points takes about a second.
    @property
    def tree(self):
        if self._tree is None:
            from scipy.spatial import cKDTree

            self._tree = cKDTree(self.points)
        return self._tree

    @property
    def tolerance(self):
        if self._tolerance is None:
            if len(self.points) > 1:
                spacing, _ = self.tree.query(self.points, k=2)
                self._tolerance = 1.5 * float(np.median(spacing[:, 1]))
            else:
                self._tolerance = np.inf
        return self._tolerance

    @tolerance.setter
    def tolerance(self, value):
        self._tolerance = value

    @classmethod
    def from_mesh(cls, points, mesh, surface, surface_index, tolerance):
        """Use a source mesh: ``surface``/``surface_index`` come from mesh.surface_mapping."""
        from scipy.spatial import cKDTree

        dist, index = cKDTree(np.asarray(points, dtype=np.float64)).query(np.asarray(mesh.points))
        import pyvista as pv

        surface_index = np.asarray(surface_index, dtype=np.int64)
        celltypes = set(np.unique(np.asarray(mesh.celltypes)).tolist()) if hasattr(mesh, "celltypes") else set()
        if isinstance(mesh, pv.PolyData) or not celltypes & VOLUME_CELL_TYPES:
            # Surface-only (stl/obj, or a shell model): the volume comes from voxels.
            return cls(points, surface, surface_index, tolerance=tolerance)
        index = index.astype(np.int64)
        index[dist > tolerance] = -1
        volume, index = _inside_only(mesh, index)
        return cls(points, surface, surface_index, volume, index, tolerance)

    def ensure_volume(self):
        """Build the voxel grid on first use (meshless TBROMs)."""
        if self.volume is not None:
            return
        import pyvista as pv

        lo, hi = self.points.min(0), self.points.max(0)
        size = np.maximum(hi - lo, 1e-12)
        h = (np.prod(size) / VOXEL_BUDGET) ** (1.0 / 3.0)
        dims = np.maximum(np.ceil(size / h).astype(int) + 1, 2)
        spacing = size / (dims - 1)
        grid = pv.ImageData(dimensions=tuple(int(d) for d in dims), spacing=tuple(spacing), origin=tuple(lo))
        dist, index = self.tree.query(np.asarray(grid.points))
        index = index.astype(np.int64)
        # A voxel node counts as inside the data within a voxel diagonal of a TBROM point.
        self.tolerance = max(self.tolerance, float(np.linalg.norm(spacing)))
        index[dist > self.tolerance] = -1
        inside = 100.0 * (index >= 0).mean()
        # Streamlines trace on the regular grid (fast cell lookup) with zero velocity outside,
        # which simply stops them; contours use the trimmed copy.
        self._stream_volume, self._stream_index = grid, index
        self.volume, self.volume_index = _inside_only(grid, index)
        log.info("voxel grid %s (%d nodes, %.1f%% inside data)", tuple(dims), grid.n_points, inside)

    # ---- filters ------------------------------------------------------------------

    def _mapped_surface(self):
        """The surface restricted to vertices that map to TBROM points (cut once)."""
        if not hasattr(self, "_surf_inside"):
            surf, index = _inside_only(self.surface, self.surface_index)
            if not hasattr(surf, "contour") or surf.n_points == 0:
                self._surf_inside = (None, None)
            else:
                if not hasattr(surf, "lines"):  # extract_points returns an UnstructuredGrid
                    original = index
                    surf = surf.extract_surface(algorithm="dataset_surface")
                    index = original[np.asarray(surf.point_data["vtkOriginalPointIds"], dtype=np.int64)]
                self._surf_inside = (surf, index)
        return self._surf_inside

    def isolines(self, scalars, levels):
        if self.surface is None or not levels:
            return None
        surf, index = self._mapped_surface()
        if surf is None:
            return _empty_curves()
        surf = surf.copy(deep=False)
        surf.point_data["s"] = _gather(scalars, index, fill=0.0)
        lines = surf.contour(isosurfaces=levels, scalars="s")
        return _polylines(lines, "s")

    def isosurfaces(self, scalars, levels):
        if not levels:
            return None
        self.ensure_volume()
        if self.volume.n_points == 0:
            return _empty_triangles()
        vol = self.volume.copy(deep=False)
        vol.point_data["s"] = _gather(scalars, self.volume_index, fill=0.0)
        iso = vol.contour(isosurfaces=levels, scalars="s", method="contour")
        if iso.n_points == 0 or iso.n_cells == 0:
            return _empty_triangles()
        iso = iso.triangulate()
        return {
            "points": np.asarray(iso.points, dtype=np.float32),
            "faces": np.asarray(iso.faces).reshape(-1, 4)[:, 1:].astype(np.int32),
            "scalars": np.asarray(iso.point_data["s"], dtype=np.float32),
        }

    def _seed_candidates(self):
        """Cell centres of the trimmed volume (inside the data), cached."""
        if getattr(self, "_centers", None) is None:
            self._centers = np.asarray(self.volume.cell_centers().points)
        return self._centers

    def _seeds(self, count, source):
        """``count`` seed points inside the volume, from a named selection or anywhere."""
        from scipy.spatial import cKDTree

        centers = self._seed_candidates()
        rng = np.random.default_rng(0)
        if source in self.seed_sets and len(self.seed_sets[source]):
            # Named-selection points (e.g. an inlet) lie on the boundary where the tracer cannot
            # start; use the nearest cell centres inside the volume instead.
            if getattr(self, "_center_tree", None) is None:
                self._center_tree = cKDTree(centers)
            pts = self.seed_sets[source]
            pts = pts[rng.choice(len(pts), size=min(count * 4, len(pts)), replace=False)]
            # Small selections (an inlet may have a few dozen points): take several nearby
            # cell centres per point so the requested seed count can still be met.
            k = int(min(max(np.ceil(2 * count / len(pts)), 1), 64, len(centers)))
            _, nearest = self._center_tree.query(pts, k=k)
            nearest = np.unique(nearest)
            return centers[nearest[rng.choice(len(nearest), size=min(count, len(nearest)), replace=False)]]
        return centers[rng.choice(len(centers), size=min(count, len(centers)), replace=False)]

    def streamlines(self, vectors, seeds, source=None):
        vectors = np.asarray(vectors, dtype=np.float32)
        if vectors.ndim != 2 or vectors.shape[1] != 3 or seeds <= 0:
            return None
        import pyvista as pv

        self.ensure_volume()
        grid = getattr(self, "_stream_volume", None) or self.volume
        index = self._stream_index if grid is getattr(self, "_stream_volume", None) else self.volume_index
        if grid.n_points == 0:
            return _empty_curves()
        vol = grid.copy(deep=False)
        vol.point_data["v"] = _gather(vectors, index, fill=0.0)
        # Seed at cell centres: always inside the volume (TBROM points often lie on its boundary,
        # where the tracer cannot locate a starting cell).
        seed_pts = self._seeds(seeds, source)
        lines = vol.streamlines_from_source(
            pv.PolyData(seed_pts), vectors="v", integration_direction="both",
            initial_step_length=0.2, step_unit="cl", max_steps=STREAM_MAX_STEPS,
            max_length=2.0 * self._diag, terminal_speed=1e-12, compute_vorticity=False)
        if lines.n_points == 0:
            return _empty_curves()
        speed = np.linalg.norm(np.asarray(lines.point_data["v"], dtype=np.float32), axis=1)
        lines.point_data["speed"] = speed
        return _polylines(lines, "speed")


def _inside_only(volume, index):
    """Keep cells whose nodes all map to the data; returns (volume, index for its nodes)."""
    valid = index >= 0
    if valid.all():
        return volume, index
    if not valid.any():
        import pyvista as pv

        return pv.UnstructuredGrid(), np.zeros(0, np.int64)
    sub = volume.extract_points(np.flatnonzero(valid), adjacent_cells=False, include_cells=True)
    original = np.asarray(sub.point_data["vtkOriginalPointIds"], dtype=np.int64)
    return sub, index[original]


def _gather(values, index, fill):
    values = np.asarray(values, dtype=np.float32)
    shape = (len(index),) + values.shape[1:]
    out = np.full(shape, fill, dtype=np.float32)
    ok = index >= 0
    out[ok] = values[index[ok]]
    return out


def _polylines(poly, scalar_name):
    """pyvista lines -> ``{"points", "counts", "scalars"}`` with one entry per polyline."""
    if poly.n_points == 0 or poly.n_lines == 0:
        return _empty_curves()
    conn = np.asarray(poly.lines)
    counts, order = [], []
    i = 0
    while i < len(conn):
        n = int(conn[i])
        counts.append(n)
        order.append(conn[i + 1:i + 1 + n])
        i += n + 1
    order = np.concatenate(order)
    return {
        "points": np.asarray(poly.points, dtype=np.float32)[order],
        "counts": np.asarray(counts, dtype=np.int32),
        "scalars": np.asarray(poly.point_data[scalar_name], dtype=np.float32)[order],
    }


def _empty_curves():
    return {"points": np.zeros((0, 3), np.float32), "counts": np.zeros(0, np.int32),
            "scalars": np.zeros(0, np.float32)}


def _empty_triangles():
    return {"points": np.zeros((0, 3), np.float32), "faces": np.zeros((0, 3), np.int32),
            "scalars": np.zeros(0, np.float32)}


class PostWorker:
    """Computes the enabled outputs for the newest frame on a background thread.

    ``emit(header, payload, latest_only=True)`` delivers ``post`` messages.
    """

    def __init__(self, emit, pack_arrays):
        self._emit = emit
        self._pack = pack_arrays
        self._cond = threading.Condition()
        self._frame = None      # frame waiting to be processed
        self._last = None       # newest frame seen, kept so a settings change can recompute it
        self._domains = {}
        self._settings = {}
        self._stop = False
        self._thread = threading.Thread(target=self._run, name="twin-post", daemon=True)
        self._thread.start()

    def set_domains(self, domains):
        with self._cond:
            self._domains = dict(domains)
            self._frame = None
            self._last = None

    def configure(self, settings):
        """``{"contours": n, "isolines": bool, "isosurfaces": n | [levels], "streamlines": seeds,
        "seed": named selection | None, "range": [vmin, vmax] | None}`` (missing keys keep their value)."""
        with self._cond:
            self._settings.update(settings)
            if self._last is not None:
                self._frame = self._last
            self._cond.notify()

    @property
    def active(self):
        s = self._settings
        return bool(s.get("isolines") or s.get("isosurfaces") or s.get("streamlines"))

    def submit(self, t, step, fields):
        with self._cond:
            self._last = (t, step, fields)
            if self.active and self._domains:
                self._frame = self._last
                self._cond.notify()

    def close(self):
        with self._cond:
            self._stop = True
            self._cond.notify()
        self._thread.join(timeout=5)

    def _run(self):
        next_ok = 0.0
        while True:
            with self._cond:
                while not self._stop and self._frame is None:
                    self._cond.wait()
                if self._stop:
                    return
                frame, self._frame = self._frame, None
                domains, settings = dict(self._domains), dict(self._settings)
            delay = next_ok - time.perf_counter()
            if delay > 0:
                time.sleep(delay)  # throttle; a newer frame may replace this one meanwhile
                with self._cond:
                    if self._frame is not None:
                        continue
            t0 = time.perf_counter()
            try:
                self._compute(frame, domains, settings)
            except Exception:
                log.exception("post-processing failed")
            next_ok = t0 + max(MIN_INTERVAL_S, time.perf_counter() - t0)

    def _compute(self, frame, domains, settings):
        t, step, fields = frame
        arrays, items = {}, []
        for rom, values in fields.items():
            dom = domains.get(rom)
            if dom is None:
                continue
            scalars = _scalars(values)
            rng = settings.get("range")
            vmin, vmax = (rng if rng else (float(np.nanmin(scalars)), float(np.nanmax(scalars))))
            outputs = {}
            if settings.get("isolines"):
                outputs[ISOLINES] = dom.isolines(scalars, contour_levels(vmin, vmax, int(settings.get("contours", 9))))
            iso = settings.get("isosurfaces")
            if iso:
                levels = list(iso) if isinstance(iso, (list, tuple)) else contour_levels(vmin, vmax, int(iso))
                outputs[ISOSURFACES] = dom.isosurfaces(scalars, levels)
            if settings.get("streamlines"):
                outputs[STREAMLINES] = dom.streamlines(values, int(settings["streamlines"]), settings.get("seed"))
            for kind, out in outputs.items():
                if out is None:
                    continue
                items.append({"rom": rom, "kind": kind, "range": [vmin, vmax]})
                for key, arr in out.items():
                    arrays[f"{rom}/{kind}/{key}"] = arr
        meta, payload = self._pack(arrays)
        self._emit({"type": "post", "t": t, "step": step, "items": items,
                    "settings": settings, "arrays": meta}, payload, latest_only=True)
