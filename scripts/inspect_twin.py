"""Print inputs/outputs/TBROM details of .twin files and time a few steps.

Run with the worker venv:
    twin_worker\\.venv\\Scripts\\python scripts\\inspect_twin.py models\\*.twin
"""

import glob
import sys
import time

import numpy as np
from pytwin import TwinModel, read_binary


def disk_snapshot(tm, rom):
    """Fallback for TBROMs whose mode coefficients are not twin outputs:
    let the runtime write snapshots (``<rom>_store_snapshots``) and read the file."""
    store = [p for p in tm.parameters if str(p).endswith("_store_snapshots")]
    if not store:
        raise RuntimeError("no in-memory snapshot and no *_store_snapshots parameter")
    tm.initialize_evaluation(parameters={str(p): 1 for p in store})
    path = tm.get_snapshot_filepath(rom, tm.evaluation_time)
    return np.asarray(read_binary(path)), f"disk via {store[0]}"


def describe(path, steps=10, step_size=0.1):
    print(f"\n=== {path}")
    tm = TwinModel(path)
    print(f"inputs     ({len(tm.inputs)}): {dict(list(tm.inputs.items())[:10])}")
    print(f"outputs    ({len(tm.outputs)}): {list(tm.outputs)[:10]}")
    print(f"parameters ({len(tm.parameters)}): {list(tm.parameters)[:10]}")
    print(f"tbroms     ({tm.tbrom_count}): {tm.tbrom_names}")

    tm.initialize_evaluation()
    for rom in tm.tbrom_names:
        print(f"  [{rom}]")
        print(f"    field inputs : {tm.get_field_input_names(rom)}")
        print(f"    field output : {tm.get_field_output_name(rom)}")
        print(f"    named sel.   : {tm.get_named_selections(rom)}")
        print(f"    data location: {tm.get_tbrom_data_location(rom)}")
        try:
            print(f"    geometry file: {tm.get_geometry_filepath(rom)}")
        except Exception as exc:
            print(f"    geometry file: n/a ({exc})")
        try:
            pts = tm.generate_points(rom, on_disk=False)
            pts = np.asarray(pts)
            print(f"    points       : shape={pts.shape} dtype={pts.dtype}")
        except Exception as exc:
            print(f"    points       : n/a ({exc})")
        try:
            snap = np.asarray(tm.generate_snapshot(rom, on_disk=False))
            mode = "memory"
        except Exception as exc:
            print(f"    snapshot     : in-memory n/a ({str(exc).splitlines()[0][:120]})")
            snap, mode = disk_snapshot(tm, rom)
        print(f"    snapshot     : [{mode}] shape={snap.shape} dtype={snap.dtype} "
              f"min={snap.min():.4g} max={snap.max():.4g}")

    t0 = time.perf_counter()
    for _ in range(steps):
        tm.evaluate_step_by_step(step_size=step_size)
    t1 = time.perf_counter()
    for rom in tm.tbrom_names:
        try:
            tm.generate_snapshot(rom, on_disk=False)
        except Exception:
            read_binary(tm.get_snapshot_filepath(rom, tm.evaluation_time))
    t2 = time.perf_counter()
    print(f"step: {(t1 - t0) / steps * 1000:.2f} ms/step, "
          f"snapshot(all roms): {(t2 - t1) * 1000:.2f} ms")
    print(f"outputs after {steps} steps: {dict(list(tm.outputs.items())[:10])}")
    tm.close()


if __name__ == "__main__":
    paths = [p for arg in sys.argv[1:] for p in glob.glob(arg)]
    for p in paths:
        describe(p)
