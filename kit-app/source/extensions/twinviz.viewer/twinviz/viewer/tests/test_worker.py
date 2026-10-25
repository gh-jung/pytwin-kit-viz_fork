# 통합 테스트: 워커 준비 → hello → ping 왕복 → 종료를 검증한다. 워커 venv가 필요하다.
import asyncio

import omni.kit.app
import omni.kit.test

from twinviz.viewer.client import TwinClient
from twinviz.viewer.extension import _resolve_worker_paths
from twinviz.viewer.process_manager import WorkerProcess


class TestWorkerRoundTrip(omni.kit.test.AsyncTestCase):
    async def test_start_ping_stop(self):
        ext_id = omni.kit.app.get_app().get_extension_manager().get_enabled_extension_id("twinviz.viewer")
        python_exe, worker_root = _resolve_worker_paths(ext_id)
        worker = WorkerProcess(python_exe, worker_root)
        client = TwinClient()
        loop = asyncio.get_running_loop()
        worker.start()
        try:
            port = await loop.run_in_executor(None, worker.wait_ready)
            hello = await client.connect(port)
            self.assertEqual(hello["type"], "hello")
            for _ in range(3):
                latency = await client.ping()
                self.assertLess(latency, 1.0)
            await client.shutdown_worker()
            await loop.run_in_executor(None, worker.stop)
            self.assertFalse(worker.running)
        finally:
            await client.close()
            worker.stop(timeout=1.0)
