"""USD scene loading helpers; overrides stay in the session layer."""

from pxr import Usd, UsdGeom


def default_basis_curves(stage):
    """Set every composed BasisCurves purpose to default, including instances.

    Instance descendants cannot be edited, so only instance ancestors containing
    curves are expanded. Source USD files are never written by this operation.
    """
    predicate = Usd.TraverseInstanceProxies(Usd.PrimAllPrimsPredicate)
    expanded = 0
    with Usd.EditContext(stage, stage.GetSessionLayer()):
        while True:
            instances = set()
            for prim in Usd.PrimRange.Stage(stage, predicate):
                if not prim.IsA(UsdGeom.BasisCurves) or not prim.IsInstanceProxy():
                    continue
                ancestor = prim.GetParent()
                while ancestor and not ancestor.IsPseudoRoot():
                    if ancestor.IsInstance() and not ancestor.IsInstanceProxy():
                        instances.add(ancestor.GetPath())
                        break
                    ancestor = ancestor.GetParent()
            if not instances:
                break
            for path in instances:
                stage.GetPrimAtPath(path).SetInstanceable(False)
                expanded += 1

        paths = []
        for prim in stage.TraverseAll():
            if prim.IsA(UsdGeom.BasisCurves):
                if not UsdGeom.Imageable(prim).CreatePurposeAttr().Set(UsdGeom.Tokens.default_):
                    raise RuntimeError(f"Could not set purpose: {prim.GetPath()}")
                paths.append(str(prim.GetPath()))
    return paths, expanded
