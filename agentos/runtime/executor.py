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


class Memory:
    """The p.6 memory API: six kinds behind four verbs, backend invisible.

    Kinds: working (private, dies with the process), scratchpad (same, for
    throwaway notes), shared (cross-agent, through the kernel only), longterm
    and semantic (keyed by agent name — they survive restarts), episodic
    (your own history; read-only).
    """

    def __init__(self, ctx: "Context") -> None:
        self._ctx = ctx

    async def store(self, key: str, value: Any, kind: str = "working") -> None:
        """Store a JSON-serializable value. Storing to `shared` publishes a
        MemoryUpdated event; `semantic` values must be text."""
        await self._ctx._syscall("memory", op="store", key=key, value=value, kind=kind)

    async def retrieve(
        self,
        key: str | None = None,
        kind: str = "working",
        query: str | None = None,
        top: int = 3,
        limit: int = 20,
    ) -> Any:
        """Fetch a value (None if absent or not yours to read). With no key:
        every key you can read, as a dict. kind="semantic" with query=... does
        similarity search; kind="episodic" returns your own recent history."""
        return await self._ctx._syscall(
            "memory", op="retrieve", key=key, kind=kind, query=query, top=top, limit=limit
        )

    async def share(self, key: str, with_agent: Any = "*") -> None:
        """Promote one of your working keys into shared memory (or widen the
        access list of a shared key you created). `with_agent` is a pid, an
        agent name, or "*" for everyone."""
        await self._ctx._syscall("memory", op="share", key=key, with_agent=with_agent)

    async def delete(self, key: str, kind: str = "working") -> bool:
        return await self._ctx._syscall("memory", op="delete", key=key, kind=kind)


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
        self._memory = Memory(self)

    @property
    def pid(self) -> int:
        return self._proc.pid

    @property
    def name(self) -> str:
        return self._proc.name

    async def _syscall(self, op: str, /, **args: Any) -> Any:
        # `op` is positional-only so syscall payloads may themselves have an
        # "op" key (request_tool does) without colliding with this parameter.
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

    async def checkpoint(self, label: str | None = None) -> int:
        """Take an explicit checkpoint (p.9's kernel.checkpoint()).

        Every completed syscall is already a checkpoint, so this is never
        required for recovery. It is here for the agent that wants to mark a
        durable point by name — the state becomes Checkpointing while it
        happens, which is what makes that lifecycle state observable.
        """
        return await self._syscall("checkpoint", label=label)

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

    # -- memory (Phase 5, p.6) ------------------------------------------------
    @property
    def memory(self) -> Memory:
        """memory.store() / retrieve() / share() / delete(). See Memory."""
        return self._memory

    # -- models (Phase 5, p.7) --------------------------------------------------
    async def request_model(
        self,
        need: str,
        prompt: str,
        system: str | None = None,
        max_tokens: int = 1024,
    ) -> dict[str, Any]:
        """Ask for a capability class ('fast', 'reasoning'), never a model name.

            reply = await ctx.request_model("fast", prompt="Summarize: ...")
            reply["text"], reply["model"], reply["cost"]

        The kernel routes to the first available candidate in the models
        config, falls to the next on failure, and records tokens and cost
        against this agent (see `agent ps`). State: Waiting.
        """
        result = await self._syscall(
            "request_model", need=need, prompt=prompt, system=system, max_tokens=max_tokens
        )
        model = result["model"]
        if model["error"] is not None:
            raise KernelError(model["error"])
        return model["value"]

    # -- tools (Phase 4, p.6-7) ---------------------------------------------
    async def request_tool(self, capability: str, op: str, **params: Any) -> Any:
        """Ask the kernel to run a tool operation. State becomes Waiting.

        Agents never import tool libraries. They request a capability by name
        ('Need: sql') and the kernel dispatches to the driver that owns the
        authentication, rate limiting, retries, and error handling:

            rows = await ctx.request_tool("sql", "query", query="SELECT ...")

        The call is refused before dispatch unless this agent's name holds the
        capability in the permission matrix — and the denial is audit-logged.
        Tool failures arrive as KernelError, never as a raw stack trace.
        """
        result = await self._syscall(
            "request_tool", capability=capability, op=op, params=params
        )
        tool = result["tool"]
        if tool["error"] is not None:
            raise KernelError(tool["error"])
        return tool["value"]

    # -- human approval (Phase 3, p.5-6) -----------------------------------
    async def request_approval(self, role: str, reason: str) -> dict[str, Any]:
        """Block until a human with `role` approves. State becomes Blocked.

        The human is a node in the dependency graph — identical in kind to an
        agent, an event, or a timer. Someone grants it from another terminal:

            agent approve <pid> --as "<role>"

        Returns {"role": ..., "reason": ..., "by": ...}. The approval is a
        durable kernel object: it survives a runtime restart, and a grant
        issued while the runtime is down is honored when it comes back.
        """
        result = await self._syscall("request_approval", role=role, reason=reason)
        return result["approval"]

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
        result = await self._syscall(
            "wait_all",
            agents=list(agents or []),
            events=list(events or []),
            timer=timer,
        )
        # JSON object keys are strings, so pids arrive as "2" when the reply
        # crossed a real pipe (process isolation). Normalize: agents always
        # see int pids, whichever transport carried the reply.
        result["agents"] = {int(pid): r for pid, r in result["agents"].items()}
        return result


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
