"""Limit how many TBROM points are computed, sent and drawn.

A TBROM with tens of millions of points cannot be streamed or drawn per frame,
and pytwin reconstructs the *whole* field on every ``evaluate_step_by_step``.
Above a point budget we keep a fixed random sample of points:

* ``shrink`` (dynamic TBROMs): restrict pytwin's ``TbRom`` to the sample, so its
  own per-step reconstruction (including the output transformations) only covers
  those points. This touches pytwin internals and is verified before it is used.
* ``slice`` (fallback, and static TBROMs whose snapshots come from disk): compute
  or read the full field, then send only the sample.
"""

import logging

import numpy as np

log = logging.getLogger(__name__)

SHRINK = "shrink"
SLICE = "slice"


def sample_indices(n, k, seed=0):
    """Sorted, reproducible random subset of ``k`` out of ``n`` point indices."""
    rng = np.random.default_rng(seed)
    return np.sort(rng.choice(n, size=k, replace=False)).astype(np.int64)


def shrink_tbrom(tbrom, sel):
    """Restrict a pytwin ``TbRom`` to the points ``sel``. Returns False (untouched) if unsupported."""
    try:
        import pyvista as pv

        basis = getattr(tbrom, "_outbasis", None)
        pdata = getattr(tbrom, "_pointsdata", None)
        n = int(tbrom.nb_points)
        if basis is None or pdata is None or getattr(tbrom, "_meshdata", None):
            return False
        axis = 2 if tbrom.isparamfieldhist else 1
        if basis.ndim <= axis or basis.shape[axis] != n or pdata.n_points != n:
            log.warning("unexpected TBROM layout for %s; falling back to slicing", tbrom.name)
            return False

        reduced = pv.PolyData(np.asarray(pdata.points)[sel])
        for name in pdata.point_data.keys():
            reduced[name] = np.asarray(pdata.point_data[name])[sel]
        new_basis = np.ascontiguousarray(np.take(basis, sel, axis=axis))
    except Exception:
        log.exception("could not shrink TBROM; falling back to slicing")
        return False

    # Swap only after everything was built, so a failure leaves pytwin consistent.
    tbrom._outbasis = new_basis
    tbrom._pointsdata = reduced
    tbrom._nbpoints = len(sel)
    if tbrom.field_output_name in reduced.point_data:
        reduced.set_active_scalars(tbrom.field_output_name)
    return True
