"""One agent, one OS process (Phase 7, p.8). Run by ProcessExecutor, never by hand.

The remarkable thing about this file is what is NOT in it: a new agent API.
The Context in here is the same class agents have used since Phase 1 — its
mailbox and inbox just end at a pipe now instead of the kernel's queues. An
agent cannot tell which side of a fork it is running on, because its entire
world was always a serializable message boundary.

Protocol, JSON lines over stdio:
    parent -> child:  {"pid":..., "name":..., "spec":...}      (header, once)
    child  -> parent: {"type": "syscall", "op", "req_id", "args"}
    parent -> child:  {"req_id":..., "value":..., "error":...}
    child  -> parent: {"type": "finished", "result"} | {"type": "failed", "error"}
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


def _emit(fh, payload: dict) -> None:
    fh.write(json.dumps(payload) + "\n")
    fh.flush()


async def main() -> int:
    # Claim the real stdout for the protocol, then point sys.stdout at stderr:
    # an agent that print()s must not be able to corrupt the framing.
    protocol = os.fdopen(os.dup(sys.stdout.fileno()), "w", encoding="utf-8")
    sys.stdout = sys.stderr

    header = json.loads(sys.stdin.readline())
    agent = agent_from_spec(header["spec"])

    # The same Context, fed by the same shapes — only the transport differs.
    proc = SimpleNamespace(
        pid=header["pid"], name=header["name"], inbox=asyncio.Queue()
    )
    mailbox: asyncio.Queue = asyncio.Queue()
    ctx = Context(proc, mailbox)
    loop = asyncio.get_running_loop()

    async def uplink() -> None:  # Context puts Syscalls here; ship them out
        while True:
            call = await mailbox.get()
            _emit(
                protocol,
                {"type": "syscall", "op": call.op, "req_id": call.req_id, "args": call.args},
            )

    def downlink() -> None:  # kernel replies come back on stdin (daemon thread)
        while True:
            line = sys.stdin.readline()
            if not line:
                os._exit(1)  # the runtime died; there is nobody left to talk to
            msg = json.loads(line)
            loop.call_soon_threadsafe(
                proc.inbox.put_nowait,
                Reply(req_id=msg["req_id"], value=msg.get("value"), error=msg.get("error")),
            )

    up = asyncio.create_task(uplink())
    threading.Thread(target=downlink, daemon=True).start()

    _RUNNING.set(id(agent))
    try:
        result = await agent.run(ctx)
        _emit(protocol, {"type": "finished", "result": result})
        return 0
    except (TypeError, ValueError) as exc:  # includes a non-serializable result
        _emit(protocol, {"type": "failed", "error": f"{type(exc).__name__}: {exc}"})
        return 1
    except BaseException as exc:  # agent bug: reported, never swallowed
        _emit(protocol, {"type": "failed", "error": f"{type(exc).__name__}: {exc}"})
        return 1
    finally:
        up.cancel()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
