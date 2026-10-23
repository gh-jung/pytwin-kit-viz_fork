"""The point budget must not change values: sampled fields equal the full field at those points."""

import unittest
from pathlib import Path

import numpy as np

from twin_worker import protocol as P
from twin_worker.decimate import SHRINK, SLICE, sample_indices
from twin_worker.runner import TwinRunner

MODELS = Path(__file__).resolve().parents[2] / "models"
FEA = MODELS / "FEADeformationTBROM_23R2.twin"
THERMAL = MODELS / "ThermalTBROM_23R2.twin"


class Collector:
    def __init__(self):
        self.msgs = []

    def __call__(self, header, payload=b"", latest_only=False):
        self.msgs.append((header, payload))

    def last(self, msg_type):
        for header, payload in reversed(self.msgs):
            if header["type"] == msg_type:
                return header, P.unpack_arrays(header.get("arrays", []), payload, np)
        raise AssertionError(f"no {msg_type}")


def run(path, max_points, inputs, steps):
    """Drive a runner synchronously; return (model_info, geometry, last frame fields)."""
    out = Collector()
    runner = TwinRunner(out)
    try:
        runner._handle({"type": P.MSG_LOAD, "path": str(path), "max_points": max_points})
        runner._handle({"type": P.MSG_SET_INPUTS, "inputs": inputs})
        if steps:
            runner._handle({"type": P.MSG_START, "step_size": 0.05, "realtime": False})
            for _ in range(steps):
                runner._tick()
        info, _ = out.last(P.MSG_MODEL_INFO)
        _, geom = out.last(P.MSG_GEOMETRY)
        _, fields = out.last(P.MSG_FRAME)
        return info, geom, fields
    finally:
        runner._unload()


class DecimateTest(unittest.TestCase):
    def test_sample_indices(self):
        sel = sample_indices(1000, 100)
        self.assertEqual(len(np.unique(sel)), 100)
        self.assertTrue((np.diff(sel) > 0).all())
        np.testing.assert_array_equal(sel, sample_indices(1000, 100))

    @unittest.skipUnless(FEA.is_file(), f"missing {FEA}")
    def test_shrink_matches_full_field(self):
        inputs = {"Force_X_Component": 4.0}
        info, geom, fields = run(FEA, 300, inputs, steps=8)
        rom = info["tbroms"][0]["name"]
        self.assertEqual(info["tbroms"][0]["decimation"], SHRINK)
        self.assertEqual((info["tbroms"][0]["points"], info["tbroms"][0]["shown"]), (1332, 300))

        _, full_geom, full_fields = run(FEA, None, inputs, steps=8)
        sel = sample_indices(1332, 300)
        np.testing.assert_allclose(geom[rom], full_geom[rom][sel], rtol=0, atol=0)
        np.testing.assert_allclose(fields[rom], full_fields[rom][sel], rtol=1e-6, atol=1e-12)

    @unittest.skipUnless(THERMAL.is_file(), f"missing {THERMAL}")
    def test_slice_static_matches_full_field(self):
        inputs = {"main_inlet_temperature": 290.0, "side_inlet_temperature": 360.0}
        info, geom, fields = run(THERMAL, 5000, inputs, steps=0)
        rom = info["tbroms"][0]["name"]
        self.assertEqual(info["tbroms"][0]["decimation"], SLICE)

        _, full_geom, full_fields = run(THERMAL, None, inputs, steps=0)
        sel = sample_indices(139868, 5000)
        np.testing.assert_array_equal(geom[rom], full_geom[rom][sel])
        np.testing.assert_array_equal(fields[rom], full_fields[rom][sel])


if __name__ == "__main__":
    unittest.main()
