"""AgentOS - an operating system-inspired runtime for autonomous AI agents."""

from .agents.base import Agent, DirectInvocationError
from .kernel.depgraph import Deadlock
from .kernel.events import KERNEL_EVENTS
from .kernel.kernel import Kernel
from .kernel.states import AgentState, InvalidTransition
from .runtime.executor import Context, KernelError

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
]
__version__ = "0.2.0"  # Phase 2: event bus, dependency graph, deadlock detection
