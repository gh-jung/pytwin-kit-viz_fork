"""USD scene loading helpers; overrides stay in the session layer."""

from pxr import Usd, UsdGeom


def default_basis_curves(stage):
    """Set every composed BasisCurves purpose to default, including instances.

    Instance descendants cannot be edited, so only instance ancestors containing
    curves are expanded. Source USD files are never written by this operation.
    """
    # 일반 순회에서 생략되는 인스턴스 내부와 비활성 prim까지 탐색 대상으로 포함한다.
    predicate = Usd.TraverseInstanceProxies(Usd.PrimAllPrimsPredicate)
    expanded = 0
    with Usd.EditContext(stage, stage.GetSessionLayer()):
        # 중첩 인스턴스는 바깥부터 해제해야 하므로 더 이상 수정 불가 proxy가 없을 때까지 반복한다.
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

        # session layer에 명시적 default 의견을 기록해 상위 prim의 purpose 상속도 덮어쓴다.
        paths = []
        for prim in stage.TraverseAll():
            if prim.IsA(UsdGeom.BasisCurves):
                if not UsdGeom.Imageable(prim).CreatePurposeAttr().Set(UsdGeom.Tokens.default_):
                    raise RuntimeError(f"Could not set purpose: {prim.GetPath()}")
                paths.append(str(prim.GetPath()))
    return paths, expanded
