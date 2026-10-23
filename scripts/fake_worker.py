"""Stand-in worker that streams a synthetic large TBROM, for stress tests without a real twin.

Speaks the real protocol: hello, then on ``load`` sends model_info, geometry,
an optional mesh_map and frames at a fixed rate until ``pause``/``shutdown``.

    python scripts/fake_worker.py --points 1000000 --mesh --vector --outputs 200 --rate 30

Prints ``TWIN_WORKER_READY port=N`` like the real worker.
"""

import argparse
import asyncio
import os
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "twin_worker"))
from twin_worker import protocol as P  # noqa: E402

ROM = "BigROM"


def surface(n_points):
    """A wavy square sheet with ~n_points vertices and two triangles per quad."""
    side = max(int(np.sqrt(n_points)), 2)
    u, v = np.meshgrid(np.linspace(0, 1, side, dtype=np.float32), np.linspace(0, 1, side, dtype=np.float32))
    pts = np.stack([u, v, 0.05 * np.sin(6 * u) * np.cos(6 * v)], axis=-1).reshape(-1, 3)
    i = np.arange(side - 1)
    a = (i[:, None] * side + i[None, :]).ravel()
    faces = np.concatenate([np.stack([a, a + 1, a + side], 1), np.stack([a + 1, a + side + 1, a + side], 1)])
    return pts.astype(np.float32), faces.astype(np.int32)


async def serve(args):
    points, faces = surface(args.points)
    n = len(points)
    outputs = [f"out_{k}" for k in range(args.outputs)]
    rng = np.random.default_rng(0)
    phase = rng.random(n, dtype=np.float32) * 6.28

    async def on_client(reader, writer):
        await P.write_message(writer, {"type": P.MSG_HELLO, "protocol": P.PROTOCOL_VERSION,
                                       "worker_version": "fake", "pid": os.getpid()})
        running = False
        step = 0

        async def send_frame():
            t = step / args.rate
            if args.vector:
                vals = np.stack([np.sin(phase + t), np.cos(phase + t), np.zeros(n, np.float32)], 1) * 0.01
            else:
                vals = np.sin(phase + t) * 50 + 300
            meta, payload = P.pack_arrays({ROM: vals.astype(np.float32)})
            await P.write_message(writer, {
                "type": P.MSG_FRAME, "t": t, "step": step, "inputs": {"x": 1.0},
                "scalars": {o: float(step) for o in outputs}, "arrays": meta}, payload)

        async def stream():
            nonlocal step
            while running:
                step += 1
                await send_frame()
                await asyncio.sleep(1.0 / args.rate)

        task = None
        try:
            while True:
                header, _ = await P.read_message(reader)
                kind = header["type"]
                if kind == P.MSG_PING:
                    await P.write_message(writer, {"type": P.MSG_PONG, "seq": header.get("seq")})
                elif kind == P.MSG_LOAD:
                    await P.write_message(writer, {
                        "type": P.MSG_MODEL_INFO, "path": "fake", "name": "fake", "mode": "dynamic",
                        "inputs": {"x": 1.0}, "outputs": outputs, "parameters": {},
                        "tbroms": [{"name": ROM, "field": "F", "location": "Nodal",
                                    "field_inputs": [], "named_selections": []}]})
                    meta, payload = P.pack_arrays({ROM: points})
                    await P.write_message(writer, {"type": P.MSG_GEOMETRY, "arrays": meta}, payload)
                    if args.mesh:
                        meta, payload = P.pack_arrays({f"{ROM}/points": points, f"{ROM}/faces": faces,
                                                       f"{ROM}/index": np.arange(n, dtype=np.int32)})
                        stats = {ROM: {"path": "fake.rst", "vertices": n, "triangles": len(faces),
                                       "unmapped": 0, "max_distance": 0.0, "tolerance": 1.0}}
                        await P.write_message(writer, {"type": P.MSG_MESH_MAP, "stats": stats, "arrays": meta},
                                              payload)
                    await send_frame()  # like the real worker: initial frame right after load
                elif kind == P.MSG_START:
                    running = True
                    task = asyncio.ensure_future(stream())
                elif kind in (P.MSG_PAUSE, P.MSG_STOP):
                    running = False
                elif kind == P.MSG_SHUTDOWN:
                    break
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            running = False
            if task:
                task.cancel()
            writer.close()
            server.close()

    server = await asyncio.start_server(on_client, "127.0.0.1", 0)
    print(f"{P.READY_PREFIX} port={server.sockets[0].getsockname()[1]}", flush=True)
    try:
        await server.serve_forever()
    except asyncio.CancelledError:
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--points", type=int, default=1_000_000)
    ap.add_argument("--mesh", action="store_true")
    ap.add_argument("--vector", action="store_true")
    ap.add_argument("--outputs", type=int, default=50)
    ap.add_argument("--rate", type=float, default=30.0)
    asyncio.run(serve(ap.parse_args()))


if __name__ == "__main__":
    main()
