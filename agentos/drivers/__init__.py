"""The driver registry: capability name -> the driver class that owns it.

Adding a driver is registering it here; no agent and no kernel code changes.
"""

from __future__ import annotations

from .base import ToolDriver, ToolError, Transient
from .browser import Browser
from .filesystem import Filesystem
from .http import Http
from .python import Python
from .shell import Shell
from .sql import SQL

REGISTRY: dict[str, type[ToolDriver]] = {
    "filesystem": Filesystem,
    "shell": Shell,
    "python": Python,
    "sql": SQL,
    "http": Http,
    "browser": Browser,
}

__all__ = [
    "REGISTRY",
    "ToolDriver",
    "ToolError",
    "Transient",
    "Filesystem",
    "Shell",
    "Python",
    "SQL",
    "Http",
    "Browser",
]
