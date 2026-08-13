"""The process table: what the kernel knows about every agent."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from .states import LEGAL_TRANSITIONS, TERMINAL, AgentState, InvalidTransition


@dataclass(slots=True)
class AgentProcess:
    """One running agent, as the kernel sees it (see AgentOS.pdf p.3)."""

    pid: int
    name: str
    parent: int | None
    spec: dict[str, Any]
    priority: str = "Normal"
    state: AgentState = AgentState.READY
    children: list[int] = field(default_factory=list)
    waiting_on: str | None = None
    result: Any = None
    exit_reason: str | None = None
    started_at: float = field(default_factory=time.time)
    ended_at: float | None = None
    checkpoint: int = 0
    model: str | None = None
    permissions: list[str] = field(default_factory=list)
    retries: int = 0
    root: int = 0
    # Dollars this task may spend on models; None is unmetered. Set on the
    # root, inherited by looking up the tree.
    budget_usd: float | None = None
    # None is unrestricted; a list (including an empty one) the kernel enforces.
    publishes: list[str] | None = None
    subscribes: list[str] | None = None

    # Runtime-only handles. Never cross the message boundary, never persisted.
    task: asyncio.Task | None = field(default=None, repr=False, compare=False)
    inbox: asyncio.Queue = field(
        default_factory=asyncio.Queue, repr=False, compare=False
    )
    pending: Any = field(default=None, repr=False, compare=False)
    pause_requested: bool = field(default=False, repr=False, compare=False)
    # Op of the blocking syscall, so its reply is journaled under that name.
    current_op: str | None = field(default=None, repr=False, compare=False)
    timer: asyncio.TimerHandle | None = field(
        default=None, repr=False, compare=False
    )

    @property
    def alive(self) -> bool:
        return self.state not in TERMINAL

    @property
    def runtime(self) -> float:
        return (self.ended_at or time.time()) - self.started_at

    def row(self) -> dict[str, Any]:
        """The serializable view: what `agent ps` reads and what recovery needs.

        Raw timestamps, not an elapsed time: a row is written at the last
        transition, and only the reader knows what time it is now.
        """
        return {
            "pid": self.pid,
            "name": self.name,
            "parent": self.parent,
            "children": len(self.children),
            "status": self.state.value,
            "priority": self.priority,
            "waiting_on": self.waiting_on,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "exit_reason": self.exit_reason,
            "spec": self.spec,
            "result": self.result,
            "checkpoint": self.checkpoint,
            "model": self.model,
            "permissions": list(self.permissions),
            "retries": self.retries,
            "root": self.root,
            "budget_usd": self.budget_usd,
            "publishes": None if self.publishes is None else list(self.publishes),
            "subscribes": None if self.subscribes is None else list(self.subscribes),
        }


class ProcessTable:
    """PID allocation plus the only place a process state may legally change."""

    def __init__(self) -> None:
        self._procs: dict[int, AgentProcess] = {}
        self._next_pid = 1
        self.on_transition = None  # set by the kernel: (proc, frm, to) -> None

    def create(
        self,
        name: str,
        spec: dict[str, Any],
        parent: int | None = None,
        priority: str = "Normal",
    ) -> AgentProcess:
        pid = self._next_pid
        self._next_pid += 1
        proc = AgentProcess(
            pid=pid, name=name, parent=parent, spec=spec, priority=priority
        )
        proc.root = pid if parent is None else self._procs[parent].root
        self._procs[pid] = proc
        if parent is not None:
            self._procs[parent].children.append(pid)
        return proc

    def restore(
        self,
        pid: int,
        name: str,
        parent: int | None,
        spec: dict[str, Any],
        priority: str,
        state: AgentState,
        started_at: float,
    ) -> AgentProcess:
        """Re-insert a process from persisted state (crash recovery)."""
        proc = AgentProcess(
            pid=pid, name=name, parent=parent, spec=spec, priority=priority
        )
        proc.state = state
        proc.started_at = started_at
        proc.root = pid if parent is None else self._procs.get(
            parent, proc).root or pid
        self._procs[pid] = proc
        self._next_pid = max(self._next_pid, pid + 1)
        if parent is not None and parent in self._procs:
            self._procs[parent].children.append(pid)
        return proc

    def get(self, pid: int) -> AgentProcess:
        try:
            return self._procs[pid]
        except KeyError:
            raise KeyError(f"no such process: pid {pid}") from None

    def all(self) -> list[AgentProcess]:
        return list(self._procs.values())

    def transition(
        self, proc: AgentProcess, to: AgentState, *, waiting_on: str | None = None
    ) -> None:
        frm = proc.state
        if to not in LEGAL_TRANSITIONS[frm]:
            raise InvalidTransition(proc.pid, frm, to)
        proc.state = to
        proc.waiting_on = waiting_on
        if to in TERMINAL:
            proc.ended_at = time.time()
        if self.on_transition is not None:
            self.on_transition(proc, frm, to)
