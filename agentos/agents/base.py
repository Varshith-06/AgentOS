"""What an agent is, from the application's side."""

from __future__ import annotations

import functools
from contextvars import ContextVar
from typing import Any

from ..kernel.messages import assert_serializable

#: Set by the executor to the identity of the agent it is currently running.
#: An agent that reaches into another agent's run() will not match it.
_RUNNING: ContextVar[int | None] = ContextVar("agentos_running_agent", default=None)


class DirectInvocationError(Exception):
    """An agent tried to call another agent directly (AgentOS.pdf p.5)."""


class Agent:
    """Subclass and implement `run`.

    Constructor params must be JSON-serializable: they are the agent's spec, and
    the spec is what lets the kernel re-create this agent in a subprocess
    (Phase 7) or after a crash (Phase 6). An agent that cannot be described as
    data cannot be checkpointed, so the constraint is enforced at spawn time
    rather than discovered later.

    `run` is wrapped so that it can only be entered by the executor. "Agents
    never directly invoke other agents" is the sentence on p.5 that the entire
    event bus exists to serve; leaving it as a convention would mean the first
    person in a hurry quietly turns AgentOS back into a function-call framework.
    """

    priority: str = "Normal"

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        run = cls.__dict__.get("run")
        if run is None or getattr(run, "_agentos_guarded", False):
            return

        @functools.wraps(run)
        async def guarded(self: Agent, ctx: Any, *args: Any, **kw: Any) -> Any:
            if _RUNNING.get() != id(self):
                raise DirectInvocationError(
                    f"{type(self).__name__}.run() was called directly. Agents are "
                    "processes, not functions: spawn it with ctx.spawn() and let "
                    "the scheduler run it, or publish an event it subscribes to."
                )
            return await run(self, ctx, *args, **kw)

        guarded._agentos_guarded = True  # type: ignore[attr-defined]
        cls.run = guarded  # type: ignore[method-assign]

    def __init__(self, **params: Any) -> None:
        assert_serializable(f"{type(self).__name__} params", params)
        self.params = params

    @property
    def name(self) -> str:
        return type(self).__name__

    async def run(self, ctx: Any) -> Any:  # pragma: no cover - interface
        raise NotImplementedError(f"{type(self).__name__}.run() is not implemented")


def spec_of(agent: Agent) -> dict[str, Any]:
    """The data needed to re-create this agent from scratch."""
    cls = type(agent)
    return {
        "module": cls.__module__,
        "qualname": cls.__qualname__,
        "name": agent.name,
        "priority": getattr(agent, "priority", "Normal"),
        "params": getattr(agent, "params", {}),
    }
