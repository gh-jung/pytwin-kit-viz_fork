"""asyncio TCP server: accepts one Kit client at a time and bridges it to the runner."""

import asyncio
import collections
import logging
import os

from . import __version__
from . import protocol as P
from .runner import TwinRunner

log = logging.getLogger(__name__)

# Commands forwarded to the runner thread.
RUNNER_COMMANDS = {P.MSG_LOAD, P.MSG_INIT, P.MSG_START, P.MSG_PAUSE, P.MSG_SET_INPUTS, P.MSG_STOP,
                   P.MSG_SET_POST}
# Messages replayed to a client that connects after a model was loaded.
REPLAYED = (P.MSG_MODEL_INFO, P.MSG_GEOMETRY, P.MSG_MESH_MAP)


class WorkerServer:
    def __init__(self, host="127.0.0.1", port=0):
        self.host = host
        self.port = port
        self._server = None
        self._client = None  # active StreamWriter
        self._shutdown = asyncio.Event()
        self._loop = None

        # Outgoing messages: reliable ones are queued in order, frames keep only the latest.
        self._outbox = collections.deque()
        self._latest = {}  # msg type -> newest (header, payload) for LATEST_ONLY types
        self._wakeup = asyncio.Event()
        self._sender_task = None
        self._replay = {}
        self.frames_dropped = 0

        self.runner = TwinRunner(self._emit_threadsafe)

    async def start(self):
        self._loop = asyncio.get_running_loop()
        self.runner.start()
        self._server = await asyncio.start_server(self._on_client, self.host, self.port)
        self.port = self._server.sockets[0].getsockname()[1]
        log.info("listening on %s:%d", self.host, self.port)
        return self.port

    async def serve_until_shutdown(self):
        await self._shutdown.wait()
        if self._client is not None:
            self._client.close()
        self._server.close()
        await self._server.wait_closed()
        await self._loop.run_in_executor(None, self.runner.close)

    def request_shutdown(self):
        self._shutdown.set()

    # ---- outgoing ----------------------------------------------------------

    def _emit_threadsafe(self, header, payload=b"", latest_only=False):
        self._loop.call_soon_threadsafe(self._enqueue, header, payload, latest_only)

    def _enqueue(self, header, payload, latest_only):
        if header["type"] in REPLAYED:
            self._replay[header["type"]] = (header, payload)
        if self._client is None:
            return
        if latest_only:
            if header["type"] in self._latest:
                self.frames_dropped += 1
            self._latest[header["type"]] = (header, payload)
        else:
            self._outbox.append((header, payload))
        self._wakeup.set()

    async def _sender(self, writer):
        while True:
            await self._wakeup.wait()
            self._wakeup.clear()
            while self._outbox:
                await P.write_message(writer, *self._outbox.popleft())
            while self._latest:
                _, message = self._latest.popitem()
                await P.write_message(writer, *message)

    # ---- incoming ----------------------------------------------------------

    async def _on_client(self, reader, writer):
        if self._client is not None:
            log.warning("rejecting second client")
            await P.write_message(writer, {"type": P.MSG_ERROR, "message": "worker already has a client"})
            writer.close()
            return
        self._client = writer
        log.info("client connected: %s", writer.get_extra_info("peername"))
        try:
            await P.write_message(writer, {
                "type": P.MSG_HELLO,
                "protocol": P.PROTOCOL_VERSION,
                "worker_version": __version__,
                "pid": os.getpid(),
            })
            self._outbox.clear()
            self._latest = {}  # msg type -> newest (header, payload) for LATEST_ONLY types
            for msg_type in REPLAYED:
                if msg_type in self._replay:
                    self._outbox.append(self._replay[msg_type])
            self._wakeup.set()
            self._sender_task = asyncio.ensure_future(self._sender(writer))
            while not self._shutdown.is_set():
                header, payload = await P.read_message(reader)
                await self._dispatch(writer, header, payload)
        except (asyncio.IncompleteReadError, ConnectionError):
            log.info("client disconnected")
        except P.ProtocolError as exc:
            log.error("protocol error: %s", exc)
        finally:
            if self._sender_task is not None:
                self._sender_task.cancel()
                self._sender_task = None
            self._client = None
            writer.close()

    async def _dispatch(self, writer, header, payload):
        msg_type = header["type"]
        if msg_type == P.MSG_PING:
            # Through the outbox so it stays ordered with other outgoing messages.
            self._enqueue({"type": P.MSG_PONG, "seq": header.get("seq")}, b"", False)
        elif msg_type in RUNNER_COMMANDS:
            self.runner.submit(header)
        elif msg_type == P.MSG_SHUTDOWN:
            log.info("shutdown requested by client")
            self.request_shutdown()
        else:
            self._enqueue({
                "type": P.MSG_ERROR,
                "message": f"unsupported message type: {msg_type}",
            }, b"", False)
