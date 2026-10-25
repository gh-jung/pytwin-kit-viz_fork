"""USD-only tests; no model files or worker required."""

import importlib.util
from pathlib import Path
import unittest

from pxr import Usd, UsdGeom


_spec = importlib.util.spec_from_file_location(
    "twinviz_usd_loader_test", Path(__file__).parents[1] / "usd_loader.py")
_loader = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_loader)


class TestUsdLoader(unittest.TestCase):
    def test_nested_inactive_and_inherited_purpose(self):
        stage = Usd.Stage.CreateInMemory()
        parent = UsdGeom.Xform.Define(stage, "/World")
        parent.CreatePurposeAttr().Set(UsdGeom.Tokens.render)
        a = UsdGeom.BasisCurves.Define(stage, "/World/A")
        b = UsdGeom.BasisCurves.Define(stage, "/World/Nested/B")
        b.CreatePurposeAttr().Set(UsdGeom.Tokens.proxy)
        b.GetPrim().SetActive(False)
        mesh = UsdGeom.Mesh.Define(stage, "/World/Mesh")
        mesh.CreatePurposeAttr().Set(UsdGeom.Tokens.guide)
        root_before = stage.GetRootLayer().ExportToString()
        paths, expanded = _loader.default_basis_curves(stage)
        self.assertEqual(set(paths), {"/World/A", "/World/Nested/B"})
        self.assertEqual(expanded, 0)
        self.assertEqual(a.ComputePurpose(), UsdGeom.Tokens.default_)
        self.assertEqual(b.GetPurposeAttr().Get(), UsdGeom.Tokens.default_)
        self.assertEqual(mesh.GetPurposeAttr().Get(), UsdGeom.Tokens.guide)
        self.assertEqual(stage.GetRootLayer().ExportToString(), root_before)

    def test_instance_curves_are_overridden_in_session(self):
        asset = Usd.Stage.CreateInMemory()
        UsdGeom.Xform.Define(asset, "/Asset")
        curve = UsdGeom.BasisCurves.Define(asset, "/Asset/Curves")
        curve.CreatePurposeAttr().Set(UsdGeom.Tokens.render)
        stage = Usd.Stage.CreateInMemory()
        instance = stage.DefinePrim("/Instance")
        instance.GetReferences().AddReference(asset.GetRootLayer().identifier, "/Asset")
        instance.SetInstanceable(True)
        root_before = stage.GetRootLayer().ExportToString()
        paths, expanded = _loader.default_basis_curves(stage)
        self.assertEqual(paths, ["/Instance/Curves"])
        self.assertEqual(expanded, 1)
        self.assertEqual(UsdGeom.BasisCurves(stage.GetPrimAtPath(paths[0])).ComputePurpose(),
                         UsdGeom.Tokens.default_)
        self.assertEqual(curve.GetPurposeAttr().Get(), UsdGeom.Tokens.render)
        self.assertEqual(stage.GetRootLayer().ExportToString(), root_before)

    def test_no_curves(self):
        stage = Usd.Stage.CreateInMemory()
        UsdGeom.Mesh.Define(stage, "/Mesh")
        self.assertEqual(_loader.default_basis_curves(stage), ([], 0))


if __name__ == "__main__":
    unittest.main()
