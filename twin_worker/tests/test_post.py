"""Iso-lines, isosurfaces and streamlines from the post-processing thread."""

import shutil
import tempfile
import time
import unittest
from pathlib import Path

import numpy as np

from twin_worker import protocol as P
from twin_worker.post import contour_levels
from twin_worker.runner import TwinRunner

MODELS = Path(__file__).resolve().parents[2] / "models"
FEA = MODELS / "FEADeformationTBROM_23R2.twin"
FEA_MESH = MODELS / "FEA_deformation.rst"
THERMAL = MODELS / "ThermalTBROM_23R2.twin"
THERMAL_MESH = MODELS / "ThermalTBROM.dat"


class Collector:
    def __init__(self):
        self.msgs = []

    def __call__(self, header, payload=b"", latest_only=False):
        self.msgs.append((header, payload))

    def wait_post(self, timeout=60.0):
        t0 = time.perf_counter()
        while time.perf_counter() - t0 < timeout:
            for header, payload in reversed(self.msgs):
                if header["type"] == P.MSG_POST:
                    return header, P.unpack_arrays(header["arrays"], payload, np)
            time.sleep(0.05)
        raise AssertionError("no post message")


def session(path, meshes, settings, inputs=None, steps=0):
    out = Collector()
    runner = TwinRunner(out)
    runner._handle({"type": P.MSG_LOAD, "path": str(path), "meshes": meshes})
    if inputs:
        runner._handle({"type": P.MSG_SET_INPUTS, "inputs": inputs})
    if steps:
        runner._handle({"type": P.MSG_START, "step_size": 0.05, "realtime": False})
        for _ in range(steps):
            runner._tick()
    out.msgs.clear()
    runner._handle({"type": P.MSG_SET_POST, "settings": settings})
    return runner, out


class PostTest(unittest.TestCase):
    def test_contour_levels(self):
        self.assertEqual(contour_levels(0.0, 10.0, 4), [2.0, 4.0, 6.0, 8.0])
        self.assertEqual(contour_levels(1.0, 1.0, 4), [])

    @unittest.skipUnless(THERMAL.is_file() and THERMAL_MESH.is_file(), "missing thermal twin or mesh")
    def test_thermal_isolines_and_isosurfaces(self):
        runner, out = session(THERMAL, {"*": str(THERMAL_MESH)},
                              {"isolines": True, "contours": 5, "isosurfaces": 2},
                              inputs={"main_inlet_temperature": 290.0, "side_inlet_temperature": 360.0})
        try:
            header, arrays = out.wait_post()
            kinds = {i["kind"] for i in header["items"]}
            self.assertEqual(kinds, {"isolines", "isosurfaces"})
            rom = header["items"][0]["rom"]
            lo, hi = header["items"][0]["range"]
            self.assertAlmostEqual(lo, 290.0, delta=2.0)
            self.assertAlmostEqual(hi, 360.0, delta=2.0)

            counts = arrays[f"{rom}/isolines/counts"]
            pts = arrays[f"{rom}/isolines/points"]
            self.assertGreater(len(counts), 0)
            self.assertEqual(counts.sum(), len(pts))
            # Every iso-line vertex carries one of the requested levels.
            levels = np.array(contour_levels(lo, hi, 5))
            vals = arrays[f"{rom}/isolines/scalars"]
            self.assertLess(np.abs(vals[:, None] - levels[None, :]).min(axis=1).max(), 1e-3 * (hi - lo))

            faces = arrays[f"{rom}/isosurfaces/faces"]
            self.assertGreater(len(faces), 100)
            self.assertLess(faces.max(), len(arrays[f"{rom}/isosurfaces/points"]))
        finally:
            runner._unload()
            runner._post.close()

    @unittest.skipUnless(FEA.is_file() and FEA_MESH.is_file(), "missing FEA twin or mesh")
    def test_fea_shell_mesh_uses_voxels_for_streamlines(self):
        # FEA_deformation.rst is a shell model (quads): surface from the mesh, volume from voxels.
        runner, out = session(FEA, {"*": str(FEA_MESH)}, {"streamlines": 50},
                              inputs={"Force_X_Component": 5.0}, steps=5)
        try:
            header, arrays = out.wait_post()
            rom = header["items"][0]["rom"]
            counts = arrays[f"{rom}/streamlines/counts"]
            pts = arrays[f"{rom}/streamlines/points"]
            self.assertTrue(runner._post._domains[rom].voxel)
            self.assertGreater(len(counts), 5)
            self.assertEqual(counts.sum(), len(pts))
            self.assertGreater(arrays[f"{rom}/streamlines/scalars"].max(), 0.0)
        finally:
            runner._unload()
            runner._post.close()

    @unittest.skipUnless(FEA.is_file(), f"missing {FEA}")
    def test_meshless_voxel_grid(self):
        # A copy without the sidecar, so the TBROM has no mesh.
        with tempfile.TemporaryDirectory() as d:
            twin = Path(d) / "fea.twin"
            shutil.copy(FEA, twin)
            runner, out = session(twin, None, {"isolines": True, "isosurfaces": 2, "streamlines": 30},
                                  inputs={"Force_X_Component": 5.0}, steps=5)
            try:
                header, arrays = out.wait_post()
                kinds = {i["kind"] for i in header["items"]}
                # No surface without a mesh, so no iso-lines; the voxel volume gives the rest.
                self.assertEqual(kinds, {"isosurfaces", "streamlines"})
                rom = header["items"][0]["rom"]
                self.assertGreater(len(arrays[f"{rom}/isosurfaces/faces"]), 10)
                self.assertGreater(len(arrays[f"{rom}/streamlines/counts"]), 5)
            finally:
                runner._unload()
                runner._post.close()


if __name__ == "__main__":
    unittest.main()
