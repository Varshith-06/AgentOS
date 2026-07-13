"""The executor: runs agent code and mediates every syscall it makes.

The kernel decides *what* runs; the executor is *how* it runs. Today that means
an asyncio task per agent. In Phase 7 it can mean an OS subprocess, and nothing
in agents/ or kernel/ has to change — because the only thing an agent ever
touches is the Context below, and the only thing a Context does is put a
serializable Syscall on a queue and wait for a Reply.
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable

from ..agents.base import _RUNNING
from ..kernel.messages import Reply, Syscall
from ..kernel.process import AgentProcess


class KernelError(Exception):
    """A syscall was rejected by the kernel."""


class Context:
    """The only handle an agent has on the world.

    Deliberately tiny. There is no `ctx.kernel`, no `ctx.processes`, and no way
    to reach another agent — an agent cannot even name one except by PID, and a
    PID is just an integer it can pass back to the kernel.
    """

    def __init__(self, proc: AgentProcess, mailbox: asyncio.Queue) -> None:
        self._proc = proc
        self._mailbox = mailbox
        self._req_id = 0

    @property
    def pid(self) -> int:
        return self._proc.pid

    @property
    def name(self) -> str:
        return self._proc.name

    async def _syscall(self, op: str, **args: Any) -> Any:
        self._req_id += 1
        call = Syscall(pid=self._proc.pid, op=op, req_id=self._req_id, args=args)
        await self._mailbox.put(call)
        reply: Reply = await self._proc.inbox.get()
        if reply.req_id != call.req_id:
            raise KernelError(
                f"reply/syscall mismatch on pid {self._proc.pid}: "
                f"expected {call.req_id}, got {reply.req_id}"
            )
        if reply.error:
            raise KernelError(reply.error)
        return reply.value

    # -- processes (Phase 1) ---------------------------------------------
    async def spawn(self, agent: Any) -> int:
        """Create a child agent. Returns its PID immediately; does not block."""
        from ..agents.base import spec_of

        return await self._syscall("spawn", spec=spec_of(agent))

    async def sleep(self, seconds: float) -> None:
        """Yield the execution slot for `seconds`. State becomes Sleeping."""
        await self._syscall("sleep", seconds=seconds)

    async def wait(self, pid: int) -> Any:
        """Block until `pid` terminates; returns its result. State: Waiting."""
        result = await self.wait_all(agents=[pid])
        return result["agents"][pid]

    async def log(self, message: str) -> None:
        await self._syscall("log", message=message)

    # -- events (Phase 2, p.4-5) -----------------------------------------
    async def publish(self, event_type: str, **payload: Any) -> None:
        """Announce that something happened. You do not know who is listening."""
        await self._syscall("publish", event_type=event_type, payload=payload)

    async def subscribe(self, *event_types: str) -> None:
        """Register interest. Events that fire while you are busy are buffered."""
        await self._syscall("subscribe", event_types=list(event_types))

    async def wait_event(self, event_type: str) -> dict[str, Any]:
        """Block until an event of this type arrives. Returns its payload."""
        result = await self.wait_all(events=[event_type])
        return result["events"][event_type]

    # -- the dependency graph (Phase 2, p.5) ------------------------------
    async def wait_all(
        self,
        agents: list[int] | None = None,
        events: list[str] | None = None,
        timer: float | None = None,
    ) -> dict[str, Any]:
        """Block until *every* dependency resolves, then wake automatically.

            await ctx.wait_all(agents=[market, legal], events=["HumanApproved"])

        This is the p.5 dependency graph as an API: the agent states what it
        needs, and the scheduler — not the application — decides when it runs
        again. Returns {"agents": {pid: result}, "events": {type: payload},
        "timer": True}.
        """
        return await self._syscall(
            "wait_all",
            agents=list(agents or []),
            events=list(events or []),
            timer=timer,
        )


class Executor:
    """Owns the asyncio tasks. The kernel never creates one directly."""

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
        ctx = Context(proc, self._mailbox)
        task = asyncio.create_task(self._run(proc, agent, ctx), name=f"agent-{proc.pid}")
        proc.task = task
        return task

    async def _run(self, proc: AgentProcess, agent: Any, ctx: Context) -> None:
        # Each asyncio task gets its own context, so this token identifies
        # exactly one running agent and cannot leak into another's.
        _RUNNING.set(id(agent))
        try:
            result = await agent.run(ctx)
        except asyncio.CancelledError:
            # Killed by the kernel. Reported, not swallowed.
            self._on_fail(proc, asyncio.CancelledError("killed"))
            raise
        except Exception as exc:  # agent bug: the kernel survives it
            self._on_fail(proc, exc)
        else:
            self._on_finish(proc, result)
