"""The dependency graph (AgentOS.pdf p.5).

Instead of static workflows, a waiting agent declares what it is waiting *for* —
other agents, events, timers (and, from Phase 3, human approvals). When the last
dependency resolves, the scheduler wakes it automatically. Nobody wrote a
sequence; the graph decided the order.

The graph is also where deadlock is caught. A cycle of waiters is detected the
moment it would be created and reported to the agent that closed it, rather than
being discovered later as a run that never ends.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

AGENT = "agent"
EVENT = "event"
TIMER = "timer"
APPROVAL = "approval"  # Phase 3


def key(kind: str, name: Any) -> str:
    return f"{kind}:{name}"


class Deadlock(Exception):
    """A wait that would close a cycle in the wait-for graph."""


@dataclass(slots=True)
class Waiting:
    """One process's outstanding dependencies."""

    pid: int
    req_id: int
    remaining: set[str]
    results: dict[str, Any] = field(default_factory=dict)

    @property
    def satisfied(self) -> bool:
        return not self.remaining


class DependencyGraph:
    def __init__(self) -> None:
        self.waiting: dict[int, Waiting] = {}
        #: dependency key -> pids blocked on it. The scheduler reads this to see
        #: which ready process would unblock the most work if it ran next.
        self.blocked_on: dict[str, set[int]] = {}

    # -- queries ---------------------------------------------------------
    def is_waiting(self, pid: int) -> bool:
        return pid in self.waiting

    def dependents(self, dep_key: str) -> set[int]:
        return self.blocked_on.get(dep_key, set())

    def agent_dependents(self) -> dict[int, int]:
        """pid -> how many processes are blocked on that pid finishing."""
        return {
            int(k.split(":", 1)[1]): len(pids)
            for k, pids in self.blocked_on.items()
            if k.startswith(f"{AGENT}:") and pids
        }

    def waits_for_agents(self, pid: int) -> set[int]:
        w = self.waiting.get(pid)
        if not w:
            return set()
        return {
            int(k.split(":", 1)[1])
            for k in w.remaining
            if k.startswith(f"{AGENT}:")
        }

    def cycle_from(self, pid: int, targets: set[int]) -> list[int] | None:
        """Would `pid` waiting on `targets` close a cycle? Return the cycle."""
        for target in targets:
            path = self._path_to(target, pid, seen=set())
            if path is not None:
                return [pid, *path]
        return None

    def _path_to(self, frm: int, goal: int, seen: set[int]) -> list[int] | None:
        if frm == goal:
            return [frm]
        if frm in seen:
            return None
        seen.add(frm)
        for nxt in self.waits_for_agents(frm):
            path = self._path_to(nxt, goal, seen)
            if path is not None:
                return [frm, *path]
        return None

    # -- mutation --------------------------------------------------------
    def add(self, pid: int, req_id: int, deps: set[str]) -> Waiting:
        w = Waiting(pid=pid, req_id=req_id, remaining=set(deps))
        self.waiting[pid] = w
        for dep in deps:
            self.blocked_on.setdefault(dep, set()).add(pid)
        return w

    def resolve(self, dep_key: str, value: Any) -> list[Waiting]:
        """Mark a dependency satisfied. Returns the processes now free to run."""
        freed: list[Waiting] = []
        for pid in list(self.blocked_on.pop(dep_key, set())):
            w = self.waiting.get(pid)
            if w is None or dep_key not in w.remaining:
                continue
            w.remaining.discard(dep_key)
            w.results[dep_key] = value
            if w.satisfied:
                freed.append(self.waiting.pop(pid))
        return freed

    def cancel(self, pid: int) -> None:
        """A process died or was killed: it waits for nothing now."""
        w = self.waiting.pop(pid, None)
        if w is not None:
            for dep in w.remaining:
                holders = self.blocked_on.get(dep)
                if holders:
                    holders.discard(pid)
                    if not holders:
                        del self.blocked_on[dep]
