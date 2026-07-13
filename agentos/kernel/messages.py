"""The agent <-> kernel message boundary.

This module is the whole reason the hybrid process model works. Agents never
hold a reference to the Kernel, the process table, or each other. Every
interaction is a Syscall message that crosses a queue and comes back as a
Reply. Both are required to be JSON-serializable, which is checked at runtime.

That check is not ceremony. It is the invariant that lets Phase 7 move an agent
into a real OS subprocess without touching a line of agent code: if a syscall
payload can survive json.dumps, it can survive a pipe.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


class NotSerializable(Exception):
    """A syscall payload could not cross the process boundary."""


def assert_serializable(what: str, payload: Any) -> None:
    try:
        json.dumps(payload)
    except (TypeError, ValueError) as exc:
        raise NotSerializable(
            f"{what} payload is not JSON-serializable: {exc}. "
            "Agents may only pass plain data across the kernel boundary "
            "(no live objects, sockets, or references to other agents)."
        ) from exc


@dataclass(slots=True)
class Syscall:
    """Agent -> kernel. `op` names the kernel service being requested."""

    pid: int
    op: str
    req_id: int
    args: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        assert_serializable(f"syscall {self.op!r}", self.args)


@dataclass(slots=True)
class Reply:
    """Kernel -> agent. Delivered only once the scheduler grants a slot."""

    req_id: int
    value: Any = None
    error: str | None = None

    def __post_init__(self) -> None:
        assert_serializable("reply", self.value)
