"""Check .twin models with the real worker code and print one short line per model.

Made for testing in a closed network where results are copied back by hand:
output is ASCII, a few dozen characters per model, and never contains model
names (models are numbered M1, M2, ... in sorted file-name order).

Run with the worker venv:
    twin_worker\\.venv\\Scripts\\python scripts\\field_check.py [models_dir]

Output (legend: python scripts/field_check.py --legend):
    ENV py=3.12.10 pytwin=0.13.0 pv=0.49.1 mr=0.56.0
    M1 ok m=d r=1 p=1332 f=v l=0.4 t=1.2 nan=0 mesh=rst/2352/0.0
    M2 ERR LOAD TwinModelError: Twin model failed during instantiation
"""

import argparse
import logging
import platform
import sys
import time
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "twin_worker"))

from twin_worker import protocol as P  # noqa: E402
from twin_worker.runner import MODE_DYNAMIC, TwinRunner  # noqa: E402

LEGEND = """\
ENV  py, pytwin, pv (pyvista), mr (ansys-mapdl-reader) versions; '-' = not installed
Mn   model number (sorted file names); ok | ERR <stage> <error>
m    mode: d = dynamic (stepped), s = static (re-initialized per input change)
r    number of TBROMs
p    points per TBROM (joined with '+')
d    points actually computed/sent per TBROM when above --max-points: <n>s = pytwin shrunk
     to the sample (fast), <n>c = full field computed then sliced; '-' = all points
f    field per TBROM: s = scalar, v = vector, x<k> = k components
l    load time [s] (instantiate + init + geometry + mesh mapping)
t    d: ms per time step incl. snapshots; s: ms per input change (re-init + read)
nan  NaN/inf values in the last frame
mesh ext/triangles/unmapped% per TBROM ('-' = no mesh sidecar, ERR(<reason>) = failed)
stages: LOAD INIT STEP MESH"""

STEPS = 20


def _version(module):
    try:
        mod = __import__(module, fromlist=["__version__"])
        return getattr(mod, "__version__", "?")
    except Exception:
        return "-"


def env_line():
    return (f"ENV py={platform.python_version()} pytwin={_version('pytwin')} "
            f"pv={_version('pyvista')} mr={_version('ansys.mapdl.reader')}")


def _short(exc, limit=60):
    text = f"{type(exc).__name__}: {exc}".replace("\n", " ").replace("[TwinModelError] ", "")
    return text[:limit]


def _field_code(arr):
    if arr.ndim == 1:
        return "s"
    k = arr.shape[1]
    return "v" if k == 3 else f"x{k}"


class Collector:
    def __init__(self):
        self.msgs = []

    def __call__(self, header, payload=b"", latest_only=False):
        self.msgs.append((header, payload))

    def last(self, msg_type):
        for header, payload in reversed(self.msgs):
            if header["type"] == msg_type:
                return header, payload
        return None, None

    def errors(self):
        return [h["message"] for h, _ in self.msgs if h["type"] == P.MSG_ERROR]


def check(path, max_points):
    out = Collector()
    runner = TwinRunner(out)  # driven synchronously on this thread; its thread is never started
    stage = "LOAD"
    try:
        t0 = time.perf_counter()
        runner._handle({"type": P.MSG_LOAD, "path": str(path), "max_points": max_points})
        load_s = time.perf_counter() - t0

        info, _ = out.last(P.MSG_MODEL_INFO)
        roms = [r["name"] for r in info["tbroms"]]
        geom_h, geom_p = out.last(P.MSG_GEOMETRY)
        geom = P.unpack_arrays(geom_h["arrays"], geom_p, np) if geom_h else {}
        mesh_h, _ = out.last(P.MSG_MESH_MAP)
        mesh_errors = [m[len("mesh:"):].strip() for m in out.errors() if m.startswith("mesh:")]

        stage = "STEP"
        n_frames = len([h for h, _ in out.msgs if h["type"] == P.MSG_FRAME])
        if runner._mode == MODE_DYNAMIC:
            runner._handle({"type": P.MSG_START, "step_size": 0.05, "realtime": False})
            t0 = time.perf_counter()
            for _ in range(STEPS):
                runner._tick()
            t_ms = (time.perf_counter() - t0) / STEPS * 1000
        else:
            stage = "INIT"
            inputs = dict(runner._inputs)
            t0 = time.perf_counter()
            runner._handle({"type": P.MSG_SET_INPUTS, "inputs": inputs})
            t_ms = (time.perf_counter() - t0) * 1000
        if len([h for h, _ in out.msgs if h["type"] == P.MSG_FRAME]) <= n_frames:
            raise RuntimeError("no frame produced")

        frame_h, frame_p = out.last(P.MSG_FRAME)
        fields = P.unpack_arrays(frame_h["arrays"], frame_p, np)
        bad = sum(int((~np.isfinite(fields[r])).sum()) for r in roms if r in fields)

        if mesh_errors:
            reason = mesh_errors[-1].split(":")[0].replace(" ", "_")[:24]
            mesh = f"ERR({reason})"
        elif mesh_h:
            parts = []
            for r in roms:
                st = mesh_h["stats"].get(r)
                if st is None:
                    parts.append("-")
                else:
                    pct = 100.0 * st["unmapped"] / max(st["vertices"], 1)
                    parts.append(f"{Path(st['path']).suffix.lstrip('.')}/{st['triangles']}/{pct:.1f}")
            mesh = "+".join(parts)
        else:
            mesh = "-"

        shown = {r["name"]: r for r in info["tbroms"]}

        def dec(r):
            how = shown[r].get("decimation")
            return f"{shown[r]['shown']}{'s' if how == 'shrink' else 'c'}" if how else "-"

        return (f"ok m={'d' if runner._mode == MODE_DYNAMIC else 's'} r={len(roms)} "
                f"p={'+'.join(str(shown[r].get('points') or len(geom.get(r, []))) for r in roms)} "
                f"d={'+'.join(dec(r) for r in roms)} "
                f"f={'+'.join(_field_code(fields[r]) for r in roms if r in fields)} "
                f"l={load_s:.1f} t={t_ms:.1f} nan={bad} mesh={mesh}")
    except Exception as exc:
        errors = out.errors()
        detail = errors[-1] if errors else _short(exc)
        return f"ERR {stage} {detail[:60]}"
    finally:
        runner._unload()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("models", nargs="?", default=str(REPO / "models"),
                    help="folder with .twin files (and optional .twinviz.json sidecars)")
    ap.add_argument("--legend", action="store_true", help="print the output legend and exit")
    ap.add_argument("--max-points", type=int, default=1_000_000,
                    help="per-TBROM point budget, same as the app's max_display_points (0 = no limit)")
    args = ap.parse_args()
    if args.legend:
        print(LEGEND)
        return
    # pytwin and the worker log a lot; keep stdout to the result lines only.
    logging.disable(logging.CRITICAL)
    print(env_line(), flush=True)
    twins = sorted(Path(args.models).glob("*.twin"))
    if not twins:
        print("M0 ERR LOAD no .twin files", flush=True)
    for i, path in enumerate(twins, 1):
        print(f"M{i} {check(path, args.max_points or None)}", flush=True)


if __name__ == "__main__":
    main()
