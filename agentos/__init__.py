"""AgentOS - an operating system-inspired runtime for autonomous AI agents."""

from .agents.base import Agent, DirectInvocationError
from .drivers import ToolDriver, ToolError, Transient
from .kernel.depgraph import Deadlock
from .kernel.events import KERNEL_EVENTS
from .kernel.kernel import Kernel
from .kernel.models import ModelError, ModelManager
from .kernel.permissions import PermissionDenied, Permissions
from .kernel.states import AgentState, InvalidTransition
from .runtime.executor import Context, KernelError, Memory

__all__ = [
    "Agent",
    "AgentState",
    "Context",
    "Deadlock",
    "DirectInvocationError",
    "InvalidTransition",
    "KERNEL_EVENTS",
    "Kernel",
    "KernelError",
    "Memory",
    "ModelError",
    "ModelManager",
    "PermissionDenied",
    "Permissions",
    "ToolDriver",
    "ToolError",
    "Transient",
]
__version__ = "0.5.0"  # Phase 5: memory manager and model routing
