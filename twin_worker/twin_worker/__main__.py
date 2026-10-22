"""Entry point: ``python -m twin_worker --port 0 --parent-pid <kit pid>``."""

import argparse
import asyncio
import logging
import sys

from . import protocol as P
from .parent_watch import watch_parent
from .server import WorkerServer


def parse_args(argv=None):
    p = argparse.ArgumentParser(prog="twin_worker")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=0, help="0 = pick a free port")
    p.add_argument("--parent-pid", type=int, default=0, help="exit when this process exits")
    p.add_argument("--log-level", default="INFO")
    return p.parse_args(argv)


async def amain(args):
    server = WorkerServer(args.host, args.port)
    port = await server.start()

    if args.parent_pid:
        loop = asyncio.get_running_loop()
        watch_parent(args.parent_pid, lambda: loop.call_soon_threadsafe(server.request_shutdown))

    # Kit reads this line from stdout to learn the port.
    print(f"{P.READY_PREFIX} port={port}", flush=True)
    await server.serve_until_shutdown()


def main(argv=None):
    args = parse_args(argv)
    # Logs go to stderr so stdout stays reserved for the READY line.
    logging.basicConfig(
        level=args.log_level.upper(),
        stream=sys.stderr,
        format="[twin_worker] %(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    asyncio.run(amain(args))


if __name__ == "__main__":
    main()
