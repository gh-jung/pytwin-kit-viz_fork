"""Launch the worker as a subprocess and exercise hello/ping/shutdown.

Run: ``.venv\\Scripts\\python -m unittest discover -s tests`` from ``twin_worker/``.
"""

import asyncio
import os
import subprocess
import sys
import unittest
from pathlib import Path

from twin_worker import protocol as P

WORKER_ROOT = Path(__file__).resolve().parents[1]


def launch_worker():
    proc = subprocess.Popen(
        [sys.executable, "-m", "twin_worker", "--port", "0", "--parent-pid", str(os.getpid())],
        cwd=WORKER_ROOT,
        stdout=subprocess.PIPE,
        text=True,
    )
    line = proc.stdout.readline().strip()
    if not line.startswith(P.READY_PREFIX):
        proc.kill()
        raise RuntimeError(f"unexpected worker output: {line!r}")
    port = int(line.split("port=")[1])
    return proc, port


class PingTest(unittest.TestCase):
    def test_hello_ping_shutdown(self):
        proc, port = launch_worker()
        try:
            asyncio.run(self._session(port))
            self.assertEqual(proc.wait(timeout=10), 0)
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.stdout.close()

    async def _session(self, port):
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        hello, _ = await P.read_message(reader)
        self.assertEqual(hello["type"], P.MSG_HELLO)
        self.assertEqual(hello["protocol"], P.PROTOCOL_VERSION)

        for seq in range(3):
            await P.write_message(writer, {"type": P.MSG_PING, "seq": seq})
            pong, _ = await P.read_message(reader)
            self.assertEqual(pong, {"type": P.MSG_PONG, "seq": seq})

        await P.write_message(writer, {"type": P.MSG_SHUTDOWN})
        writer.close()


class ArrayPackTest(unittest.TestCase):
    def test_roundtrip(self):
        import numpy as np

        arrays = {
            "points": np.arange(12, dtype=np.float32).reshape(4, 3),
            "temp": np.linspace(0, 1, 4, dtype=np.float32),
        }
        meta, payload = P.pack_arrays(arrays)
        out = P.unpack_arrays(meta, payload, np)
        for name, arr in arrays.items():
            np.testing.assert_array_equal(out[name], arr)


if __name__ == "__main__":
    unittest.main()
