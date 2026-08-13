"""The agent <-> kernel message boundary."""

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
