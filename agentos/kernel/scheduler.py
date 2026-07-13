"""Scheduling policy: which READY agent gets the next execution slot (p.4).

Scheduling is based on agent state, not CPU instructions. A policy sees the whole
ready queue plus a view of the dependency graph, so it can reason about what
running an agent would *unblock* — something a CPU scheduler never gets to know.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Protocol

from .process import AgentProcess

PRIORITY_RANK = {"High": 0, "Normal": 1, "Low": 2}


@dataclass(slots=True)
class SchedulerView:
    """What the kernel tells the policy about the world beyond the ready queue."""

    #: pid -> number of processes blocked waiting on that pid to finish
    dependents: dict[int, int] = field(default_factory=dict)


class SchedulingPolicy(Protocol):
    name: str

    def pick(self, ready: deque[AgentProcess], view: SchedulerView) -> AgentProcess:
        """Choose and remove the next process to run. `ready` is never empty."""
        ...


class FIFO:
    """First submitted, first executed. Starvation-free by construction."""

    name = "fifo"

    def pick(self, ready: deque[AgentProcess], view: SchedulerView) -> AgentProcess:
        return ready.popleft()


class Priority:
    """High before Normal before Low; FIFO within a band.

    Starvation is possible by design — that is what a priority scheduler is —
    so the ageing guard below is what keeps a Low agent from waiting forever.
    """

    name = "priority"
    #: after this many picks, the oldest waiting process wins regardless of band
    ANTI_STARVATION_PICKS = 20

    def __init__(self) -> None:
        self._picks_since_oldest = 0

    def pick(self, ready: deque[AgentProcess], view: SchedulerView) -> AgentProcess:
        self._picks_since_oldest += 1
        if self._picks_since_oldest > self.ANTI_STARVATION_PICKS:
            self._picks_since_oldest = 0
            return ready.popleft()  # oldest, whatever its band

        best = min(
            range(len(ready)),
            key=lambda i: (PRIORITY_RANK.get(ready[i].priority, 1), i),
        )
        proc = ready[best]
        del ready[best]
        if PRIORITY_RANK.get(proc.priority, 1) == 0:
            return proc
        self._picks_since_oldest = 0  # a non-High ran: nobody is starving
        return proc


class DependencyAware:
    """Run whoever unblocks the most work (p.4).

    This is the policy a CPU scheduler cannot have. The kernel knows that six
    agents are blocked waiting on pid 12, so pid 12 runs before an agent nobody
    is waiting on, regardless of submission order. Ties fall back to priority,
    then to FIFO.
    """

    name = "dependency"

    def pick(self, ready: deque[AgentProcess], view: SchedulerView) -> AgentProcess:
        best = min(
            range(len(ready)),
            key=lambda i: (
                -view.dependents.get(ready[i].pid, 0),
                PRIORITY_RANK.get(ready[i].priority, 1),
                i,
            ),
        )
        proc = ready[best]
        del ready[best]
        return proc


POLICIES: dict[str, type] = {
    "fifo": FIFO,
    "priority": Priority,
    "dependency": DependencyAware,
}


def get_policy(name: str) -> SchedulingPolicy:
    try:
        return POLICIES[name]()
    except KeyError:
        known = ", ".join(sorted(POLICIES))
        raise ValueError(f"unknown scheduling policy {name!r} (have: {known})") from None
