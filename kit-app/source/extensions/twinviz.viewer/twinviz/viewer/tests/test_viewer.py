"""Load the example TBROM twins through the session and check the USD result.

Skipped when ``models/`` does not contain the example files.
"""

import time

import numpy as np
import omni.kit.app
import omni.kit.test
import omni.usd
from pxr import UsdGeom

from twinviz.viewer import colormap
from twinviz.viewer.extension import _resolve_worker_paths
from twinviz.viewer.session import TwinSession
from twinviz.viewer.stage_updater import ROOT_PATH


async def wait_until(predicate, timeout=60.0):
    app = omni.kit.app.get_app()
    t0 = time.perf_counter()
    while not predicate():
        if time.perf_counter() - t0 > timeout:
            raise TimeoutError("condition not met")
        await app.next_update_async()


def colors_of(session, view, mesh=False):
    """Colors as rendered: per-frame values may live only in Fabric."""
    return session.updater.read_attr(view.rom, "primvars:displayColor", mesh=mesh)


class TestColormap(omni.kit.test.AsyncTestCase):
    async def test_range_ends(self):
        rgb = colormap.apply(np.array([0.0, 1.0, np.nan], np.float32), 0.0, 1.0, "viridis", linear=False)
        self.assertEqual(rgb.shape, (3, 3))
        np.testing.assert_allclose(rgb[0], [0.267, 0.005, 0.329], atol=1e-3)
        np.testing.assert_allclose(rgb[1], [0.993, 0.906, 0.144], atol=1e-3)

    async def test_linear_output(self):
        srgb = colormap.apply(np.array([0.5], np.float32), 0.0, 1.0, "turbo", linear=False)
        lin = colormap.apply(np.array([0.5], np.float32), 0.0, 1.0, "turbo")
        np.testing.assert_allclose(lin, colormap.srgb_to_linear(srgb), rtol=1e-5)
        self.assertTrue((lin <= srgb + 1e-6).all())

    async def test_vector_magnitude(self):
        mags = colormap.scalarize(np.array([[3, 4, 0], [0, 0, 1]], np.float32))
        np.testing.assert_allclose(mags, [5, 1])


class TestViewer(omni.kit.test.AsyncTestCase):
    """Runs with the default Fabric backend; TestViewerUsdBackend repeats it on USD."""

    backend = "fabric"

    async def setUp(self):
        await omni.usd.get_context().new_stage_async()
        ext_id = omni.kit.app.get_app().get_extension_manager().get_enabled_extension_id("twinviz.viewer")
        python_exe, worker_root = _resolve_worker_paths(ext_id)
        self.models = worker_root.parent / "models"
        self.session = TwinSession(python_exe, worker_root, write_backend=self.backend)

    async def tearDown(self):
        await self.session.stop_worker()
        self.session.destroy()

    async def _load(self, name):
        path = self.models / name
        if not path.is_file():
            self.skipTest(f"missing {path}")
        await self.session.load(path)
        updater = self.session.updater
        await wait_until(lambda: updater.views and updater.frames_applied > 0)
        return next(iter(updater.views.values()))

    async def test_dynamic_fea_points_and_colors(self):
        self.session.updater.restyle(display="points")
        view = await self._load("FEADeformationTBROM_23R2.twin")
        stage = omni.usd.get_context().get_stage()
        points = UsdGeom.Points(stage.GetPrimAtPath(view.path))
        self.assertEqual(len(points.GetPointsAttr().Get()), 1332)
        self.assertEqual(self.session.mode, "dynamic")

        base_colors = colors_of(self.session, view)
        self.session.set_input("Force_X_Component", 5.0)
        await self.session.start(step_size=0.05, realtime=False)
        await wait_until(lambda: self.session.last_frame["step"] > 5)
        await self.session.pause()
        n = self.session.updater.frames_applied
        await wait_until(lambda: self.session.updater.frames_applied > n or not self.session.running)
        self.assertTrue(view.is_vector)
        self.assertFalse(np.allclose(colors_of(self.session, view), base_colors))

        # Deformation moves the points away from the undeformed positions.
        self.session.updater.restyle(deform_scale=1000.0)
        await wait_until(lambda: self.session.updater.settled)
        moved = self.session.updater.read_attr(view.rom, "points")
        self.assertGreater(np.abs(moved - view.base_points).max(), 0.0)

    async def test_fea_mesh_mapping(self):
        if not (self.models / "FEA_deformation.rst").is_file():
            self.skipTest("missing FEA_deformation.rst")
        view = await self._load("FEADeformationTBROM_23R2.twin")
        await wait_until(lambda: view.mesh_layer is not None)
        stage = omni.usd.get_context().get_stage()
        mesh = UsdGeom.Mesh(stage.GetPrimAtPath(view.mesh_path))
        n_vertices = len(mesh.GetPointsAttr().Get())
        self.assertEqual(len(mesh.GetFaceVertexIndicesAttr().Get()), 3 * len(mesh.GetFaceVertexCountsAttr().Get()))
        # The point cloud is only authored when shown.
        self.assertFalse(stage.GetPrimAtPath(view.path))
        self.assertEqual(self.session.mesh_stats[view.rom]["unmapped"], 0)

        self.session.set_input("Force_X_Component", 5.0)
        await self.session.start(step_size=0.05, realtime=False)
        await wait_until(lambda: self.session.last_frame["step"] > 5)
        await self.session.pause()
        n = self.session.updater.frames_applied
        await wait_until(lambda: self.session.updater.frames_applied > n)
        colors = colors_of(self.session, view, mesh=True)
        self.assertEqual(len(colors), n_vertices)
        self.assertGreater(np.ptp(colors, axis=0).max(), 0.3)  # a real gradient, not one color

        # Switching to points shows the cloud and hides the mesh.
        self.session.updater.restyle(display="points")
        await wait_until(lambda: self.session.updater.settled)
        self.assertEqual(UsdGeom.Imageable(stage.GetPrimAtPath(view.mesh_path)).ComputeVisibility(),
                         UsdGeom.Tokens.invisible)
        self.assertEqual(UsdGeom.Imageable(stage.GetPrimAtPath(view.path)).ComputeVisibility(),
                         UsdGeom.Tokens.inherited)

    async def test_worker_crash_recovery(self):
        await self._load("FEADeformationTBROM_23R2.twin")
        self.session.set_input("Force_X_Component", 3.0)
        await self.session.start(step_size=0.05, realtime=False)
        await wait_until(lambda: self.session.last_frame["step"] > 3)
        old_pid = self.session.client.hello["pid"]

        self.session.worker._proc.kill()  # simulate a crash
        await wait_until(lambda: self.session.restarts == 1 and self.session.connected
                         and self.session.client.hello["pid"] != old_pid)
        # Model, inputs and run state come back; frames keep flowing from the new worker.
        await wait_until(lambda: self.session.running and self.session.last_frame["step"] > 3
                         and self.session.last_frame["inputs"]["Force_X_Component"] == 3.0)
        self.assertFalse(self.session.recovering)
        self.assertTrue(any("exited with code" in text for _, _, text in self.session.log))

    async def test_static_thermal_colors_follow_inputs(self):
        self.session.updater.restyle(display="points")
        view = await self._load("ThermalTBROM_23R2.twin")
        self.assertEqual(self.session.mode, "static")
        self.session.set_input("main_inlet_temperature", 280.0)
        self.session.set_input("side_inlet_temperature", 400.0)
        await wait_until(lambda: self.session.last_frame["inputs"]["side_inlet_temperature"] == 400.0)
        await wait_until(lambda: abs(view.range[1] - 400.0) < 1.0)
        self.assertAlmostEqual(view.range[0], 280.0, delta=1.0)
        self.assertEqual(len(colors_of(self.session, view)), 139868)
        self.assertTrue(omni.usd.get_context().get_stage().GetPrimAtPath(ROOT_PATH))


class TestViewerUsdBackend(TestViewer):
    backend = "usd"
