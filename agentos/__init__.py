"""AgentOS - an operating system-inspired runtime for autonomous AI agents."""

from importlib import import_module
from typing import TYPE_CHECKING

# Loaded on first use, not at import: a courier or an agent process that needs
# one module should not pay for the kernel, the drivers and the client too.
_EXPORTS = {
    "Agent": ".agents.base",
    "DirectInvocationError": ".agents.base",
    "DaemonUnavailable": ".client",
    "RemoteAgentFailed": ".client",
    "RuntimeClient": ".client",
    "ToolDriver": ".drivers",
    "ToolError": ".drivers",
    "Transient": ".drivers",
    "Deadlock": ".kernel.depgraph",
    "KERNEL_EVENTS": ".kernel.events",
    "Kernel": ".kernel.kernel",
    "ModelError": ".kernel.models",
    "ModelManager": ".kernel.models",
    "PermissionDenied": ".kernel.permissions",
    "Permissions": ".kernel.permissions",
    "AgentState": ".kernel.states",
    "InvalidTransition": ".kernel.states",
    "Context": ".runtime.executor",
    "KernelError": ".runtime.executor",
    "Memory": ".runtime.executor",
}

if TYPE_CHECKING:
    from .agents.base import Agent, DirectInvocationError  # noqa: F401
    from .client import DaemonUnavailable, RemoteAgentFailed, RuntimeClient  # noqa: F401
    from .drivers import ToolDriver, ToolError, Transient  # noqa: F401
    from .kernel.depgraph import Deadlock  # noqa: F401
    from .kernel.events import KERNEL_EVENTS  # noqa: F401
    from .kernel.kernel import Kernel  # noqa: F401
    from .kernel.models import ModelError, ModelManager  # noqa: F401
    from .kernel.permissions import PermissionDenied, Permissions  # noqa: F401
    from .kernel.states import AgentState, InvalidTransition  # noqa: F401
    from .runtime.executor import Context, KernelError, Memory  # noqa: F401


def __getattr__(name: str):
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module 'agentos' has no attribute {name!r}")
    value = getattr(import_module(module, __name__), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_EXPORTS))


__all__ = sorted(_EXPORTS)
__version__ = "1.0.0"
