"""Agents as real OS processes (Phase 7, p.8).

Same executor interface as the asyncio Executor; the kernel cannot tell the
difference. Each agent gets its own interpreter running
`python -m agentos.runtime.child`; Syscall and Reply cross an actual OS pipe
as JSON lines — the Phase 1 rule ("anything that survives json.dumps survives
a pipe") cashed in literally, which is why not a line of agents/ or kernel/
had to change.

The scheduler's discipline survives intact: the child only advances when the
kernel puts a reply on `proc.inbox` (which happens when the scheduler grants a
slot), so slots, pause-at-syscall, replay after a crash — all of it works on
subprocess agents unchanged. And kill() is now literal: cancelling the pump
task kills the child process.
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections import deque
from typing import Any, Callable

from ..agents.base import spec_of
from ..kernel.messages import Syscall
from ..kernel.process import AgentProcess


class ProcessExecutor:
    """Owns one OS subprocess per agent. The kernel never sees the pipes."""

    def __init__(
        self,
        mailbox: asyncio.Queue,
        on_finish: Callable[[AgentProcess, Any], None],
        on_fail: Callable[[AgentProcess, BaseException], None],
    ) -> None:
        self._mailbox = mailbox
        self._on_finish = on_finish
        self._on_fail = on_fail

    def start(self, proc: AgentProcess, agent: Any) -> asyncio.Task:
        task = asyncio.create_task(
            self._run(proc, agent), name=f"agent-proc-{proc.pid}"
        )
        proc.task = task
        return task

    async def _run(self, proc: AgentProcess, agent: Any) -> None:
        stderr_tail: deque[str] = deque(maxlen=40)
        child = None  # a kill can land before the interpreter even exists
        pumps: list[asyncio.Task] = []
        try:
            child = await asyncio.create_subprocess_exec(
                sys.executable,
                "-X", "utf8",
                "-m", "agentos.runtime.child",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            header = {"pid": proc.pid, "name": proc.name, "spec": spec_of(agent)}
            child.stdin.write((json.dumps(header) + "\n").encode("utf-8"))
            await child.stdin.drain()

            pumps = [
                asyncio.create_task(self._feed_replies(proc, child)),
                asyncio.create_task(self._collect_stderr(child, stderr_tail)),
            ]

            while True:
                line = await child.stdout.readline()
                if not line:
                    code = await child.wait()
                    detail = " | ".join(stderr_tail) or "no stderr"
                    raise RuntimeError(
                        f"agent process died (exit {code}): {detail[-400:]}"
                    )
                msg = json.loads(line)
                if msg["type"] == "syscall":
                    await self._mailbox.put(
                        Syscall(
                            pid=proc.pid,
                            op=msg["op"],
                            req_id=msg["req_id"],
                            args=msg["args"],
                        )
                    )
                elif msg["type"] == "finished":
                    self._on_finish(proc, msg["result"])
                    return
                elif msg["type"] == "failed":
                    self._on_fail(proc, RuntimeError(msg["error"]))
                    return
        except asyncio.CancelledError:
            # Killed by the kernel — and with process isolation, "killed"
            # means the OS process actually dies.
            if child is not None:
                child.kill()
                await child.wait()
            self._on_fail(proc, asyncio.CancelledError("killed"))
            raise
        except Exception as exc:  # protocol or transport failure
            self._on_fail(proc, exc)
        finally:
            for pump in pumps:
                pump.cancel()
            if child is not None:
                if child.returncode is None:
                    child.kill()
                try:
                    await asyncio.shield(child.wait())
                except asyncio.CancelledError:
                    pass

    async def _feed_replies(self, proc: AgentProcess, child) -> None:
        """Kernel replies land on proc.inbox exactly as they always did; this
        pump is the only part that knows the inbox now ends at a pipe."""
        while True:
            reply = await proc.inbox.get()
            line = json.dumps(
                {"req_id": reply.req_id, "value": reply.value, "error": reply.error}
            )
            child.stdin.write((line + "\n").encode("utf-8"))
            await child.stdin.drain()

    @staticmethod
    async def _collect_stderr(child, tail: deque) -> None:
        while True:
            line = await child.stderr.readline()
            if not line:
                return
            tail.append(line.decode("utf-8", errors="replace").rstrip())
