"""What an agent is, from the application's side."""

from __future__ import annotations

import functools
import hashlib
import importlib
import importlib.util
import sys
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from ..kernel.messages import assert_serializable

_RUNNING: ContextVar[int | None] = ContextVar("agentos_running_agent", default=None)


class DirectInvocationError(Exception):
    """An agent tried to call another agent directly (AgentOS.pdf p.5)."""


class Agent:
    """Subclass and implement `run`."""

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
    module = sys.modules.get(cls.__module__)
    return {
        "module": cls.__module__,
        "qualname": cls.__qualname__,
        "file": getattr(module, "__file__", None),
        "name": agent.name,
        "priority": getattr(agent, "priority", "Normal"),
        "params": getattr(agent, "params", {}),
    }


_MODULE_CACHE: dict[str, Any] = {}


def agent_from_spec(spec: dict[str, Any]) -> Agent:
    """Re-create an agent from its spec — the Phase 1 discipline paying off."""
    # Keyed by file too: two applications are both "__main__" to themselves.
    cache_key = (spec["module"], spec.get("file"))
    module = _MODULE_CACHE.get(cache_key)
    if module is None:
        try:
            module = importlib.import_module(spec["module"])
            if not hasattr(module, spec["qualname"].split(".")[0]):
                raise ImportError(spec["qualname"])
        except ImportError:
            file = spec.get("file")
            if not file or not Path(file).exists():
                raise
            digest = hashlib.md5(str(Path(file).resolve()).encode()).hexdigest()[:8]
            loader = importlib.util.spec_from_file_location(
                f"agentos_loaded_{Path(file).stem}_{digest}", file
            )
            module = importlib.util.module_from_spec(loader)
            # Register before exec so spec_of() can resolve classes defined inside.
            sys.modules[module.__name__] = module
            loader.loader.exec_module(module)
            _MODULE_CACHE[(module.__name__, file)] = module
        _MODULE_CACHE[cache_key] = module
    cls = getattr(module, spec["qualname"])
    agent = cls(**spec.get("params", {}))
    if "priority" in spec:
        agent.priority = spec["priority"]
    return agent
