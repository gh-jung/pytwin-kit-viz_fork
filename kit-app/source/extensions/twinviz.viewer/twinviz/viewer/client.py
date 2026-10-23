"""Client for the twin worker.

Receiving runs on a dedicated thread with a blocking socket: a large model sends
megabytes per frame, and reading that through Kit's asyncio loop (which runs once
per app update) stalled the UI and throttled the frame rate. The reader thread
also unpacks arrays, keeps only the newest frame, and the main thread picks up
messages once per update (``pump``), so handlers always run on the main thread.
"""

import asyncio
import collections
import itertools
import socket
import threading

import carb
import carb.eventdispatcher
import numpy as np
import omni.kit.app

from . import protocol as P

SOCKET_BUFFER = 8 * 1024 * 1024


class TwinClient:
    def __init__(self):
        self.hello = None
        self.frames_dropped = 0      # frames replaced by a newer one before the main thread took them
        self._sock = None
        self._reader = None
        self._send_lock = threading.Lock()
        self._lock = threading.Lock()
        self._inbox = collections.deque()  # (header, arrays) in arrival order, except frames
        self._latest = {}   # msg type -> newest (header, arrays) for P.LATEST_ONLY types
        self._seq = itertools.count()
        self._pending_pings = {}
        self._handlers = {}
        self._pump_sub = carb.eventdispatcher.get_eventdispatcher().observe_event(
            event_name=omni.kit.app.GLOBAL_EVENT_UPDATE,
            observer_name="twinviz.viewer.TwinClient",
            on_event=lambda _: self.pump(),
        )

    @property
    def connected(self):
        return self._sock is not None

    def on(self, msg_type, handler):
        """Register ``handler(header, arrays)``; ``arrays`` is ``{name: ndarray}`` (empty if none)."""
        self._handlers[msg_type] = handler

    async def connect(self, port, host="127.0.0.1", timeout=5.0):
        def blocking_connect():
            sock = socket.create_connection((host, port), timeout=timeout)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, SOCKET_BUFFER)
            header, _ = P.recv_message(sock)
            sock.settimeout(None)
            return sock, header

        sock, header = await asyncio.get_running_loop().run_in_executor(None, blocking_connect)
        if header["type"] != P.MSG_HELLO:
            sock.close()
            raise P.ProtocolError(f"expected hello, got {header['type']}")
        if header.get("protocol") != P.PROTOCOL_VERSION:
            sock.close()
            raise P.ProtocolError(
                f"protocol mismatch: worker={header.get('protocol')} kit={P.PROTOCOL_VERSION}"
            )
        self.hello = header
        self._sock = sock
        self._reader = threading.Thread(target=self._read_loop, args=(sock,), name="twinviz-recv", daemon=True)
        self._reader.start()
        return header

    async def ping(self, timeout=2.0):
        """Round-trip a ping; returns latency in seconds."""
        loop = asyncio.get_running_loop()
        seq = next(self._seq)
        fut = loop.create_future()
        self._pending_pings[seq] = fut
        t0 = loop.time()
        try:
            await self.send({"type": P.MSG_PING, "seq": seq})
            await asyncio.wait_for(fut, timeout)
        finally:
            self._pending_pings.pop(seq, None)
        return loop.time() - t0

    async def send(self, header, payload=b""):
        sock = self._sock
        if sock is None:
            raise ConnectionError("not connected")
        # Commands are small; a blocking send does not stall the loop in practice.
        with self._send_lock:
            P.send_message(sock, header, payload)

    async def shutdown_worker(self):
        self.shutdown_worker_nowait()

    def shutdown_worker_nowait(self):
        """Best-effort shutdown request, then close."""
        sock = self._sock
        if sock is not None:
            try:
                with self._send_lock:
                    P.send_message(sock, {"type": P.MSG_SHUTDOWN})
            except OSError:
                pass
        self._close_socket(notify=False)

    async def close(self):
        self._close_socket(notify=False)

    def destroy(self):
        self._close_socket(notify=False)
        self._pump_sub = None

    def _close_socket(self, notify):
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()
        for fut in self._pending_pings.values():
            fut.cancel()
        self._pending_pings.clear()
        with self._lock:
            self._inbox.clear()
            self._latest.clear()
        if notify:
            handler = self._handlers.get("disconnected")
            if handler is not None:
                handler({"type": "disconnected"}, {})

    # ---- reader thread --------------------------------------------------------

    def _read_loop(self, sock):
        try:
            while True:
                header, payload = P.recv_message(sock)
                arrays = P.unpack_arrays(header["arrays"], payload, np) if "arrays" in header else {}
                with self._lock:
                    if header["type"] in P.LATEST_ONLY:
                        if header["type"] in self._latest:
                            self.frames_dropped += 1
                        self._latest[header["type"]] = (header, arrays)
                    else:
                        self._inbox.append((header, arrays))
        except (OSError, ConnectionError, P.ProtocolError) as exc:
            if sock is self._sock:  # not closed on purpose
                carb.log_info(f"[twinviz.viewer] worker connection closed: {exc}")
                with self._lock:
                    self._inbox.append(({"type": "_eof"}, {}))
        except Exception as exc:
            carb.log_error(f"[twinviz.viewer] receive loop failed: {exc}")
            with self._lock:
                self._inbox.append(({"type": "_eof"}, {}))

    # ---- main thread ------------------------------------------------------------

    def pump(self):
        """Dispatch received messages on the main thread (called every app update)."""
        if not self._inbox and not self._latest:
            return
        with self._lock:
            messages = list(self._inbox)
            self._inbox.clear()
            latest, self._latest = self._latest, {}
        for header, arrays in messages:
            if header["type"] == "_eof":
                if self._sock is not None:
                    self._close_socket(notify=True)
                return
            self._dispatch(header, arrays)
        for message in latest.values():
            self._dispatch(*message)

    def _dispatch(self, header, arrays):
        msg_type = header["type"]
        if msg_type == P.MSG_PONG:
            fut = self._pending_pings.get(header.get("seq"))
            if fut is not None and not fut.done():
                fut.set_result(header)
        elif msg_type == P.MSG_ERROR:
            carb.log_error(f"[twinviz.viewer] worker error: {header.get('message')}")
        handler = self._handlers.get(msg_type)
        if handler is not None:
            try:
                handler(header, arrays)
            except Exception as exc:
                carb.log_error(f"[twinviz.viewer] handler for {msg_type} failed: {exc}")
