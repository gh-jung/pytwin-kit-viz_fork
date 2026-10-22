"""asyncio client for the twin worker; runs on Kit's event loop."""

import asyncio
import itertools

import carb

from . import protocol as P


class TwinClient:
    def __init__(self):
        self.hello = None
        self._reader = None
        self._writer = None
        self._recv_task = None
        self._seq = itertools.count()
        self._pending_pings = {}
        self._handlers = {}

    @property
    def connected(self):
        return self._writer is not None and not self._writer.is_closing()

    def on(self, msg_type, handler):
        """Register ``handler(header, payload)`` for a worker message type."""
        self._handlers[msg_type] = handler

    async def connect(self, port, host="127.0.0.1", timeout=5.0):
        self._reader, self._writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout
        )
        header, _ = await asyncio.wait_for(P.read_message(self._reader), timeout)
        if header["type"] != P.MSG_HELLO:
            raise P.ProtocolError(f"expected hello, got {header['type']}")
        if header.get("protocol") != P.PROTOCOL_VERSION:
            raise P.ProtocolError(
                f"protocol mismatch: worker={header.get('protocol')} kit={P.PROTOCOL_VERSION}"
            )
        self.hello = header
        self._recv_task = asyncio.ensure_future(self._recv_loop())
        return header

    async def ping(self, timeout=2.0):
        """Round-trip a ping; returns latency in seconds."""
        loop = asyncio.get_running_loop()
        seq = next(self._seq)
        fut = loop.create_future()
        self._pending_pings[seq] = fut
        t0 = loop.time()
        try:
            await P.write_message(self._writer, {"type": P.MSG_PING, "seq": seq})
            await asyncio.wait_for(fut, timeout)
        finally:
            self._pending_pings.pop(seq, None)
        return loop.time() - t0

    async def send(self, header, payload=b""):
        await P.write_message(self._writer, header, payload)

    async def shutdown_worker(self):
        if self.connected:
            try:
                await self.send({"type": P.MSG_SHUTDOWN})
            except ConnectionError:
                pass
        await self.close()

    def shutdown_worker_nowait(self):
        """Best-effort shutdown request without awaiting; for extension teardown."""
        if self._recv_task is not None:
            self._recv_task.cancel()
            self._recv_task = None
        if self._writer is not None:
            try:
                self._writer.write(P.encode({"type": P.MSG_SHUTDOWN}))
            except Exception:
                pass
            self._writer.close()
            self._writer = None

    async def close(self):
        if self._recv_task is not None:
            self._recv_task.cancel()
            self._recv_task = None
        if self._writer is not None:
            self._writer.close()
            self._writer = None
        for fut in self._pending_pings.values():
            fut.cancel()
        self._pending_pings.clear()

    async def _recv_loop(self):
        try:
            while True:
                header, payload = await P.read_message(self._reader)
                msg_type = header["type"]
                if msg_type == P.MSG_PONG:
                    fut = self._pending_pings.get(header.get("seq"))
                    if fut is not None and not fut.done():
                        fut.set_result(header)
                elif msg_type == P.MSG_ERROR:
                    carb.log_error(f"[twinviz.viewer] worker error: {header.get('message')}")
                handler = self._handlers.get(msg_type)
                if handler is not None:
                    handler(header, payload)
        except (asyncio.IncompleteReadError, ConnectionError):
            carb.log_info("[twinviz.viewer] worker connection closed")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # keep Kit alive on unexpected worker data
            carb.log_error(f"[twinviz.viewer] receive loop failed: {exc}")
        finally:
            if self._writer is not None:
                self._writer.close()
                self._writer = None
            handler = self._handlers.get("disconnected")
            if handler is not None:
                handler({"type": "disconnected"}, b"")
