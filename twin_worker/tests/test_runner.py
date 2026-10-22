"""End-to-end tests against the example TBROM twins in ``models/``.

Skipped when the model files are not present (they are not in git).
"""

import asyncio
import unittest
from pathlib import Path

import numpy as np

from twin_worker import protocol as P

from test_ping import launch_worker

MODELS = Path(__file__).resolve().parents[2] / "models"
FEA = MODELS / "FEADeformationTBROM_23R2.twin"
THERMAL = MODELS / "ThermalTBROM_23R2.twin"
FEA_MESH = MODELS / "FEA_deformation.rst"
THERMAL_MESH = MODELS / "ThermalTBROM.dat"


class Session:
    def __init__(self, reader, writer):
        self.reader = reader
        self.writer = writer

    async def send(self, **header):
        await P.write_message(self.writer, header)

    async def expect(self, msg_type, timeout=60.0):
        """Read until a message of ``msg_type`` arrives; fail on worker errors."""
        async def loop():
            while True:
                header, payload = await P.read_message(self.reader)
                if header["type"] == P.MSG_ERROR:
                    raise AssertionError(f"worker error: {header['message']}")
                if header["type"] == msg_type:
                    arrays = P.unpack_arrays(header.get("arrays", []), payload, np)
                    return header, arrays
        return await asyncio.wait_for(loop(), timeout)


class RunnerTest(unittest.TestCase):
    def run_session(self, scenario):
        proc, port = launch_worker()
        try:
            async def main():
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
                s = Session(reader, writer)
                await s.expect(P.MSG_HELLO)
                try:
                    await scenario(s)
                finally:
                    await s.send(type=P.MSG_SHUTDOWN)
                    writer.close()
            asyncio.run(main())
            self.assertEqual(proc.wait(timeout=30), 0)
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.stdout.close()

    @unittest.skipUnless(FEA.is_file(), f"missing {FEA}")
    def test_dynamic_fea_deformation(self):
        async def scenario(s):
            await s.send(type=P.MSG_LOAD, path=str(FEA))
            info, _ = await s.expect(P.MSG_MODEL_INFO)
            self.assertEqual(info["mode"], "dynamic")
            self.assertEqual(list(info["inputs"]), ["Force_X_Component"])
            rom = info["tbroms"][0]["name"]

            _, geom = await s.expect(P.MSG_GEOMETRY)
            self.assertEqual(geom[rom].shape, (1332, 3))
            self.assertEqual(geom[rom].dtype, np.float32)

            frame0, fields0 = await s.expect(P.MSG_FRAME)
            self.assertEqual(frame0["step"], 0)
            self.assertEqual(fields0[rom].shape, (1332, 3))

            await s.send(type=P.MSG_SET_INPUTS, inputs={"Force_X_Component": 4.0})
            await s.send(type=P.MSG_START, step_size=0.05, realtime=False)
            last = None
            for _ in range(5):
                last, fields = await s.expect(P.MSG_FRAME)
            self.assertGreater(last["step"], 0)
            self.assertGreater(last["t"], 0.0)
            self.assertEqual(last["inputs"]["Force_X_Component"], 4.0)
            self.assertFalse(np.allclose(fields[rom], fields0[rom]))

            await s.send(type=P.MSG_PAUSE)
            state, _ = await s.expect(P.MSG_STATE)
            self.assertFalse(state["running"])

        self.run_session(scenario)

    @unittest.skipUnless(THERMAL.is_file(), f"missing {THERMAL}")
    def test_static_thermal(self):
        async def scenario(s):
            await s.send(type=P.MSG_LOAD, path=str(THERMAL))
            info, _ = await s.expect(P.MSG_MODEL_INFO)
            self.assertEqual(info["mode"], "static")
            rom = info["tbroms"][0]["name"]

            _, geom = await s.expect(P.MSG_GEOMETRY)
            self.assertEqual(geom[rom].shape, (139868, 3))
            await s.expect(P.MSG_FRAME)

            await s.send(type=P.MSG_SET_INPUTS,
                         inputs={"main_inlet_temperature": 280.0, "side_inlet_temperature": 400.0})
            frame, fields = await s.expect(P.MSG_FRAME)
            temp = fields[rom]
            self.assertEqual(temp.shape, (139868,))
            self.assertAlmostEqual(float(temp.min()), 280.0, delta=1.0)
            self.assertAlmostEqual(float(temp.max()), 400.0, delta=1.0)
            self.assertAlmostEqual(frame["scalars"]["MaxTemp"], float(temp.max()), delta=1.0)

        self.run_session(scenario)

    @unittest.skipUnless(FEA.is_file() and FEA_MESH.is_file(), "missing FEA twin or mesh")
    def test_mesh_mapping_fea(self):
        async def scenario(s):
            await s.send(type=P.MSG_LOAD, path=str(FEA), meshes={"*": str(FEA_MESH)})
            info, _ = await s.expect(P.MSG_MODEL_INFO)
            rom = info["tbroms"][0]["name"]
            _, geom = await s.expect(P.MSG_GEOMETRY)
            header, mesh = await s.expect(P.MSG_MESH_MAP)
            points, faces, index = mesh[f"{rom}/points"], mesh[f"{rom}/faces"], mesh[f"{rom}/index"]
            self.assertEqual(points.shape[1], 3)
            self.assertEqual(faces.shape[1], 3)
            self.assertEqual(len(index), len(points))
            self.assertLess(faces.max(), len(points))
            self.assertEqual(header["stats"][rom]["unmapped"], 0)
            # Nodal TBROM on its own mesh: every vertex maps onto a coincident point.
            np.testing.assert_allclose(geom[rom][index], points, atol=1e-6)
            await s.expect(P.MSG_FRAME)

        self.run_session(scenario)

    @unittest.skipUnless(THERMAL.is_file() and THERMAL_MESH.is_file(), "missing thermal twin or mesh")
    def test_mesh_mapping_thermal_elemental(self):
        async def scenario(s):
            await s.send(type=P.MSG_LOAD, path=str(THERMAL), meshes={"*": str(THERMAL_MESH)})
            info, _ = await s.expect(P.MSG_MODEL_INFO)
            rom = info["tbroms"][0]["name"]
            self.assertEqual(info["tbroms"][0]["location"].lower(), "elemental")
            header, mesh = await s.expect(P.MSG_MESH_MAP)
            st = header["stats"][rom]
            # Element centroids lie inside the surface; most vertices must still map.
            self.assertGreater(st["triangles"], 5000)
            self.assertLess(st["unmapped"] / st["vertices"], 0.15)
            await s.expect(P.MSG_FRAME)

        self.run_session(scenario)


if __name__ == "__main__":
    unittest.main()
