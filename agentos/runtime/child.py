"""One agent, one OS process (Phase 7, p.8). Run by ProcessExecutor, never by hand.

The remarkable thing about this file is what is NOT in it: a new agent API.
The Context in here is the same class agents have used since Phase 1 — its
mailbox and inbox just end at a transport now instead of the kernel's queues.
An agent cannot tell which side of a fork it is running on, because its
entire world was always a serializable message boundary.

The protocol (JSON lines over a loopback TCP socket):
    child  -> parent: {"token": ...}                           (handshake, once)
    parent -> child:  {"pid":..., "name":..., "spec":...}      (header, once)
    child  -> parent: {"type": "syscall", "op", "req_id", "args"}
    parent -> child:  {"req_id":..., "value":..., "error":...}
    child  -> parent: {"type": "finished", "result"} | {"type": "failed", "error"}

AGENTOS_CONNECT=host:port and AGENTOS_TOKEN arrive in the environment. The
child dials the executor and authenticates before anything else is said.
sys.stdout is pointed at stderr on the way in, so an agent that print()s
cannot be mistaken for the runtime.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from types import SimpleNamespace

from agentos.agents.base import _RUNNING, agent_from_spec
from agentos.kernel.messages import Reply
from agentos.runtime.executor import Context


def _wire(header: dict) -> tuple[SimpleNamespace, asyncio.Queue, Context]:
    """The same Context the kernel would build, fed by the same shapes."""
    proc = SimpleNamespace(
        pid=header["pid"], name=header["name"], inbox=asyncio.Queue()
    )
    mailbox: asyncio.Queue = asyncio.Queue()
    return proc, mailbox, Context(proc, mailbox)


async def _execute(agent, ctx: Context, emit) -> int:
    """Run the agent; report finished/failed on the channel. `emit` is the
    only thing here that knows where bytes go."""
    _RUNNING.set(id(agent))
    try:
        result = await agent.run(ctx)
        await emit({"type": "finished", "result": result})
        return 0
    except (TypeError, ValueError) as exc:  # includes a non-serializable result
        await emit({"type": "failed", "error": f"{type(exc).__name__}: {exc}"})
        return 1
    except BaseException as exc:  # agent bug: reported, never swallowed
        await emit({"type": "failed", "error": f"{type(exc).__name__}: {exc}"})
        return 1


def _syscall_line(call) -> dict:
    return {
        "type": "syscall", "op": call.op, "req_id": call.req_id, "args": call.args
    }


def _reply_of(msg: dict) -> Reply:
    return Reply(
        req_id=msg["req_id"], value=msg.get("value"), error=msg.get("error")
    )


async def _main(endpoint: str, token: str) -> int:
    sys.stdout = sys.stderr  # stdout is a dead end here; prints go to stderr

    host, _, port = endpoint.rpartition(":")
    reader, writer = await asyncio.open_connection(host, int(port))

    async def emit(payload: dict) -> None:
        writer.write((json.dumps(payload) + "\n").encode("utf-8"))
        await writer.drain()

    await emit({"token": token})
    header = json.loads(await reader.readline())
    agent = agent_from_spec(header["spec"])
    proc, mailbox, ctx = _wire(header)

    async def uplink() -> None:
        while True:
            await emit(_syscall_line(await mailbox.get()))

    async def downlink() -> None:
        while True:
            line = await reader.readline()
            if not line:
                os._exit(1)  # the runtime died; there is nobody left to talk to
            proc.inbox.put_nowait(_reply_of(json.loads(line)))

    up = asyncio.create_task(uplink())
    down = asyncio.create_task(downlink())
    try:
        return await _execute(agent, ctx, emit)
    finally:
        up.cancel()
        down.cancel()
        writer.close()


async def main() -> int:
    endpoint = os.environ.get("AGENTOS_CONNECT")
    if not endpoint:
        print(
            "agentos.runtime.child is spawned by the kernel, not run by hand "
            "(no AGENTOS_CONNECT in the environment)",
            file=sys.stderr,
        )
        return 2
    return await _main(endpoint, os.environ.get("AGENTOS_TOKEN", ""))


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
