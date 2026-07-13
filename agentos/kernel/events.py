"""The event bus (AgentOS.pdf p.4-5).

Agents never invoke other agents. They publish an event, and the runtime decides
who wakes. That is what produces loosely coupled agent systems: the publisher
does not know its subscribers exist, and adding a new subscriber requires
editing nothing.

Delivery is buffered per subscriber, not broadcast-and-forget. A subscriber that
is busy when an event fires still receives it, because the event lands in that
subscriber's queue and waits. Without the buffer, "did I subscribe before you
published?" becomes a race, and races in a scheduler are the bugs you never
reproduce.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any

#: The kernel-emitted event types from p.5. Applications may publish their own
#: named events too (the doc's own example, ResearchCompleted, is one of those).
KERNEL_EVENTS: frozenset[str] = frozenset(
    {
        "AgentFinished",
        "AgentFailed",
        "ToolCompleted",
        "HumanApproved",
        "MemoryUpdated",
        "ModelFinished",
        "TimerExpired",
        "FileCreated",
    }
)


class InvalidEvent(Exception):
    """A malformed event type."""


def validate(event_type: Any) -> str:
    if not isinstance(event_type, str) or not event_type.strip():
        raise InvalidEvent(f"event type must be a non-empty string, got {event_type!r}")
    return event_type


@dataclass(slots=True)
class Event:
    type: str
    payload: dict[str, Any]
    source_pid: int | None
    seq: int


@dataclass(slots=True)
class EventBus:
    #: pid -> event type -> buffered events not yet consumed by that subscriber
    inboxes: dict[int, dict[str, deque[Event]]] = field(default_factory=dict)
    history: list[Event] = field(default_factory=list)
    _seq: int = 0

    def subscribe(self, pid: int, event_type: str) -> None:
        validate(event_type)
        self.inboxes.setdefault(pid, {}).setdefault(event_type, deque())

    def subscribers(self, event_type: str) -> list[int]:
        return [pid for pid, topics in self.inboxes.items() if event_type in topics]

    def publish(
        self, event_type: str, payload: dict[str, Any], source_pid: int | None = None
    ) -> Event:
        validate(event_type)
        self._seq += 1
        event = Event(event_type, payload, source_pid, self._seq)
        self.history.append(event)
        for pid in self.subscribers(event_type):
            self.inboxes[pid][event_type].append(event)
        return event

    def consume(self, pid: int, event_type: str) -> Event | None:
        """Take the oldest buffered event of this type for this subscriber."""
        queue = self.inboxes.get(pid, {}).get(event_type)
        if queue:
            return queue.popleft()
        return None

    def forget(self, pid: int) -> None:
        self.inboxes.pop(pid, None)
