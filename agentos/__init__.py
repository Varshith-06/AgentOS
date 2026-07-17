"""AgentOS - an operating system-inspired runtime for autonomous AI agents."""

from .agents.base import Agent, DirectInvocationError
from .client import DaemonUnavailable, RemoteAgentFailed, RuntimeClient
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
    "DaemonUnavailable",
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
    "RemoteAgentFailed",
    "RuntimeClient",
    "ToolDriver",
    "ToolError",
    "Transient",
]
__version__ = "1.0.0"  # Phase 8: all eight phases of the design doc implemented
