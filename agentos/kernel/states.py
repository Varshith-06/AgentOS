"""Agent lifecycle states and the legal transitions between them.

The state machine is enforced, not advisory: an illegal transition raises
InvalidTransition rather than silently corrupting the process table.
"""

from __future__ import annotations

from enum import Enum


class AgentState(str, Enum):
    READY = "Ready"
    RUNNING = "Running"
    WAITING = "Waiting"
    SLEEPING = "Sleeping"
    BLOCKED = "Blocked"
    FINISHED = "Finished"
    FAILED = "Failed"
    CHECKPOINTING = "Checkpointing"
    SUSPENDED = "Suspended"


TERMINAL: frozenset[AgentState] = frozenset({AgentState.FINISHED, AgentState.FAILED})

#: A process may only move along these edges. Anything else is a kernel bug.
LEGAL_TRANSITIONS: dict[AgentState, frozenset[AgentState]] = {
    AgentState.READY: frozenset(
        {AgentState.RUNNING, AgentState.SUSPENDED, AgentState.FAILED}
    ),
    AgentState.RUNNING: frozenset(
        {
            AgentState.WAITING,
            AgentState.SLEEPING,
            AgentState.BLOCKED,
            AgentState.CHECKPOINTING,
            AgentState.SUSPENDED,
            AgentState.FINISHED,
            AgentState.FAILED,
        }
    ),
    # A woken process goes back to READY and waits for a scheduler slot; it does
    # not jump straight to RUNNING. That is what makes the scheduler real.
    AgentState.WAITING: frozenset(
        {AgentState.READY, AgentState.SUSPENDED, AgentState.FAILED}
    ),
    AgentState.SLEEPING: frozenset(
        {AgentState.READY, AgentState.SUSPENDED, AgentState.FAILED}
    ),
    AgentState.BLOCKED: frozenset(
        {AgentState.READY, AgentState.SUSPENDED, AgentState.FAILED}
    ),
    AgentState.CHECKPOINTING: frozenset({AgentState.RUNNING, AgentState.FAILED}),
    AgentState.SUSPENDED: frozenset({AgentState.READY, AgentState.FAILED}),
    AgentState.FINISHED: frozenset(),
    AgentState.FAILED: frozenset(),
}


class InvalidTransition(Exception):
    """Raised when the kernel attempts an illegal lifecycle transition."""

    def __init__(self, pid: int, frm: AgentState, to: AgentState) -> None:
        super().__init__(f"pid {pid}: illegal transition {frm.value} -> {to.value}")
        self.pid = pid
        self.frm = frm
        self.to = to


def can_transition(frm: AgentState, to: AgentState) -> bool:
    return to in LEGAL_TRANSITIONS[frm]
