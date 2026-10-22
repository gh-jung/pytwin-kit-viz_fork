"""Scalar -> RGB colormaps (numpy, no matplotlib)."""

import numpy as np

# Control points sampled from the matplotlib/ParaView maps; linearly interpolated.
_MAPS = {
    "turbo": [
        (0.190, 0.072, 0.232), (0.276, 0.421, 0.891), (0.158, 0.736, 0.923),
        (0.197, 0.949, 0.595), (0.644, 0.990, 0.234), (0.933, 0.812, 0.227),
        (0.984, 0.493, 0.128), (0.816, 0.185, 0.018), (0.480, 0.016, 0.011),
    ],
    "viridis": [
        (0.267, 0.005, 0.329), (0.283, 0.141, 0.458), (0.254, 0.265, 0.530),
        (0.207, 0.372, 0.553), (0.164, 0.471, 0.558), (0.128, 0.567, 0.551),
        (0.135, 0.659, 0.518), (0.267, 0.749, 0.441), (0.478, 0.821, 0.318),
        (0.741, 0.873, 0.150), (0.993, 0.906, 0.144),
    ],
    "coolwarm": [
        (0.230, 0.299, 0.754), (0.553, 0.690, 0.996), (0.865, 0.865, 0.865),
        (0.957, 0.604, 0.482), (0.706, 0.016, 0.150),
    ],
    "jet": [
        (0.0, 0.0, 0.5), (0.0, 0.0, 1.0), (0.0, 0.5, 1.0), (0.0, 1.0, 1.0),
        (0.5, 1.0, 0.5), (1.0, 1.0, 0.0), (1.0, 0.5, 0.0), (1.0, 0.0, 0.0), (0.5, 0.0, 0.0),
    ],
}

NAMES = list(_MAPS)
_LUT_SIZE = 256
_LUTS = {}


def srgb_to_linear(c):
    c = np.asarray(c, dtype=np.float32)
    return np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4).astype(np.float32)


def _lut(name, linear):
    lut = _LUTS.get((name, linear))
    if lut is None:
        ctrl = np.asarray(_MAPS[name], dtype=np.float32)
        xs = np.linspace(0.0, 1.0, len(ctrl))
        t = np.linspace(0.0, 1.0, _LUT_SIZE)
        lut = np.stack([np.interp(t, xs, ctrl[:, c]) for c in range(3)], axis=1).astype(np.float32)
        if linear:
            lut = srgb_to_linear(lut)
        _LUTS[(name, linear)] = lut
    return lut


def scalarize(values):
    """Field values (N,) or (N, k) -> (N,) float32; vectors use their magnitude."""
    values = np.asarray(values, dtype=np.float32)
    if values.ndim == 2:
        return np.linalg.norm(values, axis=1)
    return values


def apply(scalars, vmin, vmax, name="turbo", linear=True):
    """(N,) scalars -> (N, 3) float32 RGB in [0, 1].

    The maps are defined in sRGB; ``linear=True`` (default) returns linear values,
    which is what USD ``displayColor`` and RTX expect.
    """
    lut = _lut(name, linear)
    scalars = np.nan_to_num(scalars, nan=vmin, posinf=vmax, neginf=vmin)
    span = vmax - vmin
    if not np.isfinite(span) or span <= 0:
        idx = np.zeros(scalars.shape, dtype=np.int32)
    else:
        t = (scalars - vmin) * ((_LUT_SIZE - 1) / span)
        idx = np.clip(t, 0, _LUT_SIZE - 1).astype(np.int32)
    return lut[idx]
