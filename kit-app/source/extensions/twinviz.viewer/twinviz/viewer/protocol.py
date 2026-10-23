"""Wire protocol shared by the Kit extension and the twin worker.

Standard library only: this file is imported by both Kit's embedded Python and
the worker venv. The canonical copy lives in ``twin_worker/twin_worker/``;
``scripts/sync_protocol.ps1`` copies it into the Kit extension.

Frame layout (little-endian)::

    [u32 header_len][u32 payload_len][header: UTF-8 JSON][payload: raw bytes]

Array payloads are concatenated raw buffers described by ``header["arrays"]``,
a list of ``{"name", "dtype", "shape", "offset", "nbytes"}`` entries.
"""

import asyncio
import json
import struct

PROTOCOL_VERSION = 1

_PREFIX = struct.Struct("<II")
MAX_HEADER_BYTES = 16 * 1024 * 1024
MAX_PAYLOAD_BYTES = 2 * 1024 * 1024 * 1024

# Kit -> worker
MSG_PING = "ping"
MSG_LOAD = "load"
MSG_INIT = "init"
MSG_START = "start"
MSG_PAUSE = "pause"
MSG_SET_INPUTS = "set_inputs"
MSG_STOP = "stop"
MSG_SHUTDOWN = "shutdown"
MSG_SET_POST = "set_post"   # contours / isosurfaces / streamlines settings

# worker -> Kit
MSG_HELLO = "hello"
MSG_PONG = "pong"
MSG_MODEL_INFO = "model_info"
MSG_GEOMETRY = "geometry"
MSG_MESH_MAP = "mesh_map"
MSG_FRAME = "frame"
MSG_STATE = "state"
MSG_POST = "post"           # derived geometry for the newest frame

# Messages where only the newest one matters; older ones may be dropped.
LATEST_ONLY = frozenset({MSG_FRAME, MSG_POST})
MSG_ERROR = "error"
MSG_LOG = "log"

# Printed on the worker's stdout once listening, e.g. "TWIN_WORKER_READY port=50123".
READY_PREFIX = "TWIN_WORKER_READY"


class ProtocolError(Exception):
    pass


def encode(header, payload=b""):
    header_bytes = json.dumps(header, separators=(",", ":")).encode("utf-8")
    return _PREFIX.pack(len(header_bytes), len(payload)) + header_bytes + payload


async def write_message(writer: asyncio.StreamWriter, header, payload=b""):
    writer.write(encode(header, payload))
    await writer.drain()


async def read_message(reader: asyncio.StreamReader):
    """Return ``(header, payload)``. Raises ``asyncio.IncompleteReadError`` on EOF."""
    prefix = await reader.readexactly(_PREFIX.size)
    header_len, payload_len = _PREFIX.unpack(prefix)
    if header_len > MAX_HEADER_BYTES or payload_len > MAX_PAYLOAD_BYTES:
        raise ProtocolError(f"frame too large: header={header_len} payload={payload_len}")
    header = json.loads(await reader.readexactly(header_len))
    payload = await reader.readexactly(payload_len) if payload_len else b""
    if not isinstance(header, dict) or "type" not in header:
        raise ProtocolError(f"invalid header: {header!r}")
    return header, payload


def _recv_exact(sock, n):
    """Read exactly ``n`` bytes from a blocking socket into one buffer (no re-copying)."""
    buf = bytearray(n)
    view = memoryview(buf)
    got = 0
    while got < n:
        k = sock.recv_into(view[got:], n - got)
        if k == 0:
            raise ConnectionError("connection closed")
        got += k
    return buf


def recv_message(sock):
    """Blocking counterpart of :func:`read_message` for a thread that owns the socket."""
    header_len, payload_len = _PREFIX.unpack(_recv_exact(sock, _PREFIX.size))
    if header_len > MAX_HEADER_BYTES or payload_len > MAX_PAYLOAD_BYTES:
        raise ProtocolError(f"frame too large: header={header_len} payload={payload_len}")
    header = json.loads(_recv_exact(sock, header_len))
    payload = _recv_exact(sock, payload_len) if payload_len else bytearray()
    if not isinstance(header, dict) or "type" not in header:
        raise ProtocolError(f"invalid header: {header!r}")
    return header, payload


def send_message(sock, header, payload=b""):
    """Blocking counterpart of :func:`write_message`."""
    sock.sendall(encode(header, payload))


def pack_arrays(arrays):
    """Pack ``{name: ndarray}`` into ``(meta_list, payload_bytes)``.

    Duck-typed on numpy's ``dtype.str``, ``shape`` and ``tobytes()`` so this
    module does not import numpy. Arrays are sent in C order.
    """
    meta = []
    chunks = []
    offset = 0
    for name, arr in arrays.items():
        data = arr.tobytes(order="C")
        meta.append({
            "name": name,
            "dtype": arr.dtype.str,
            "shape": list(arr.shape),
            "offset": offset,
            "nbytes": len(data),
        })
        chunks.append(data)
        offset += len(data)
    return meta, b"".join(chunks)


def unpack_arrays(meta, payload, np):
    """Inverse of :func:`pack_arrays`; ``np`` is the caller's numpy module."""
    view = memoryview(payload)
    out = {}
    for m in meta:
        buf = view[m["offset"]:m["offset"] + m["nbytes"]]
        out[m["name"]] = np.frombuffer(buf, dtype=np.dtype(m["dtype"])).reshape(m["shape"])
    return out
