"""One agent, one OS process (Phase 7, p.8). Run by ProcessExecutor, never by hand.

The remarkable thing about this file is what is NOT in it: a new agent API.
The Context in here is the same class agents have used since Phase 1 — its
mailbox and inbox just end at a transport now instead of the kernel's queues.
An agent cannot tell which side of a fork it is running on, because its
entire world was always a serializable message boundary.

Two transports, one protocol (JSON lines):
    parent -> child:  {"pid":..., "name":..., "spec":...}      (header, once)
    child  -> parent: {"type": "syscall", "op", "req_id", "args"}
    parent -> child:  {"req_id":..., "value":..., "error":...}
    child  -> parent: {"type": "finished", "result"} | {"type": "failed", "error"}

stdio (default): the child's real stdout is the channel; stdin carries replies.
socket: AGENTOS_CONNECT=host:port and AGENTOS_TOKEN are in the environment.
The child dials the executor, sends {"token": ...} as its first line, and the
same lines flow over TCP. Either way, sys.stdout is pointed at stderr first,
so an agent that print()s can never corrupt the framing.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
from types import SimpleNamespace

from agentos.agents.base import _RUNNING, agent_from_spec
from agentos.kernel.messages import Reply
from agentos.runtime.executor import Context


def _wire(header: dict) -> tuple[SimpleNamespace, asyncio.Queue, Context]:
    """The same Context, fed by the same shapes — only the transport differs."""
    proc = SimpleNamespace(
        pid=header["pid"], name=header["name"], inbox=asyncio.Queue()
    )
    mailbox: asyncio.Queue = asyncio.Queue()
    return proc, mailbox, Context(proc, mailbox)


async def _execute(agent, ctx: Context, emit) -> int:
    """Run the agent; report finished/failed on the channel. Shared by both
    transports — `emit` is the only thing that knows where bytes go."""
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


async def _stdio_main() -> int:
    # Claim the real stdout for the protocol, then point sys.stdout at stderr:
    # an agent that print()s must not be able to corrupt the framing.
    protocol = os.fdopen(os.dup(sys.stdout.fileno()), "w", encoding="utf-8")
    sys.stdout = sys.stderr

    header = json.loads(sys.stdin.readline())
    agent = agent_from_spec(header["spec"])
    proc, mailbox, ctx = _wire(header)
    loop = asyncio.get_running_loop()

    async def emit(payload: dict) -> None:
        protocol.write(json.dumps(payload) + "\n")
        protocol.flush()

    async def uplink() -> None:  # Context puts Syscalls here; ship them out
        while True:
            await emit(_syscall_line(await mailbox.get()))

    def downlink() -> None:  # kernel replies come back on stdin (daemon thread)
        while True:
            line = sys.stdin.readline()
            if not line:
                os._exit(1)  # the runtime died; there is nobody left to talk to
            loop.call_soon_threadsafe(
                proc.inbox.put_nowait, _reply_of(json.loads(line))
            )

    up = asyncio.create_task(uplink())
    threading.Thread(target=downlink, daemon=True).start()
    try:
        return await _execute(agent, ctx, emit)
    finally:
        up.cancel()


async def _socket_main(endpoint: str, token: str) -> int:
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
    if endpoint:
        return await _socket_main(endpoint, os.environ.get("AGENTOS_TOKEN", ""))
    return await _stdio_main()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
