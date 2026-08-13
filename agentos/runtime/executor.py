"""Context: the only handle an agent has on the world."""

from __future__ import annotations

import asyncio
from typing import Any

from ..kernel.messages import Reply, Syscall
from ..kernel.process import AgentProcess


class KernelError(Exception):
    """A syscall was rejected by the kernel."""


class Memory:
    """The p.6 memory API: four kinds behind four verbs, backend invisible."""

    def __init__(self, ctx: "Context") -> None:
        self._ctx = ctx

    async def store(self, key: str, value: Any, kind: str = "working") -> None:
        """Store a JSON-serializable value.

        `shared` publishes MemoryUpdated; text stored to `longterm` is embedded,
        so a later retrieve(kind="longterm", query=...) can find it.
        """
        await self._ctx._syscall("memory", op="store", key=key, value=value, kind=kind)

    async def retrieve(
        self,
        key: str | None = None,
        kind: str = "working",
        query: str | None = None,
        top: int = 3,
        limit: int = 20,
    ) -> Any:
        """Fetch a value (None if absent or not yours to read). With no key:"""
        return await self._ctx._syscall(
            "memory", op="retrieve", key=key, kind=kind, query=query, top=top, limit=limit
        )

    async def share(self, key: str, with_agent: Any = "*") -> None:
        """Promote a working key into shared memory, or widen a shared key's access.

        `with_agent` is a pid, an agent name, or "*" for everyone.
        """
        await self._ctx._syscall("memory", op="share", key=key, with_agent=with_agent)

    async def delete(self, key: str, kind: str = "working") -> bool:
        return await self._ctx._syscall("memory", op="delete", key=key, kind=kind)


class Context:
    """The only handle an agent has on the world."""

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
        # `op` is positional-only so a syscall payload may carry its own "op" key.
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

    async def spawn(
        self,
        agent: Any,
        grant: list[str] | None = None,
        publishes: list[str] | None = None,
        subscribes: list[str] | None = None,
    ) -> int:
        """Create a child agent. Returns its PID immediately; does not block.

        `grant` may name any subset of your own capabilities, and never more.
        """
        from ..agents.base import spec_of

        return await self._syscall(
            "spawn", spec=spec_of(agent), grant=grant,
            publishes=publishes, subscribes=subscribes,
        )

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
        """Take an explicit checkpoint (p.9's kernel.checkpoint())."""
        return await self._syscall("checkpoint", label=label)

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

    @property
    def memory(self) -> Memory:
        """memory.store() / retrieve() / share() / delete(). See Memory."""
        return self._memory

    async def request_model(
        self,
        need: str,
        prompt: str,
        system: str | None = None,
        max_tokens: int = 1024,
    ) -> dict[str, Any]:
        """Ask for a capability class ('fast', 'reasoning'), never a model name."""
        result = await self._syscall(
            "request_model", need=need, prompt=prompt, system=system, max_tokens=max_tokens
        )
        model = result["model"]
        if model["error"] is not None:
            raise KernelError(model["error"])
        return model["value"]

    async def request_tool(self, capability: str, op: str, **params: Any) -> Any:
        """Ask the kernel to run a tool operation. State becomes Waiting."""
        result = await self._syscall(
            "request_tool", capability=capability, op=op, params=params
        )
        tool = result["tool"]
        if tool["error"] is not None:
            raise KernelError(tool["error"])
        return tool["value"]

    async def request_approval(self, role: str, reason: str) -> dict[str, Any]:
        """Block until a human with `role` approves. State becomes Blocked."""
        result = await self._syscall("request_approval", role=role, reason=reason)
        return result["approval"]

    async def wait_all(
        self,
        agents: list[int] | None = None,
        events: list[str] | None = None,
        timer: float | None = None,
    ) -> dict[str, Any]:
        """Block until *every* dependency resolves, then wake automatically."""
        result = await self._syscall(
            "wait_all",
            agents=list(agents or []),
            events=list(events or []),
            timer=timer,
        )
        # Pids arrive as strings when a reply crossed a pipe as JSON; agents
        # always see ints.
        result["agents"] = {int(pid): r for pid, r in result["agents"].items()}
        return result


