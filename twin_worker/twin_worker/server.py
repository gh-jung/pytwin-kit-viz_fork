"""asyncio TCP server: accepts one Kit client at a time and dispatches messages."""

import asyncio
import logging
import os

from . import __version__
from . import protocol as P

log = logging.getLogger(__name__)


class WorkerServer:
    def __init__(self, host="127.0.0.1", port=0):
        self.host = host
        self.port = port
        self._server = None
        self._client = None  # active StreamWriter
        self._shutdown = asyncio.Event()

    async def start(self):
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

    def request_shutdown(self):
        self._shutdown.set()

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
            while not self._shutdown.is_set():
                header, payload = await P.read_message(reader)
                await self._dispatch(writer, header, payload)
        except (asyncio.IncompleteReadError, ConnectionError):
            log.info("client disconnected")
        except P.ProtocolError as exc:
            log.error("protocol error: %s", exc)
        finally:
            self._client = None
            writer.close()

    async def _dispatch(self, writer, header, payload):
        msg_type = header["type"]
        if msg_type == P.MSG_PING:
            await P.write_message(writer, {"type": P.MSG_PONG, "seq": header.get("seq")})
        elif msg_type == P.MSG_SHUTDOWN:
            log.info("shutdown requested by client")
            self.request_shutdown()
        else:
            await P.write_message(writer, {
                "type": P.MSG_ERROR,
                "message": f"unsupported message type: {msg_type}",
            })
