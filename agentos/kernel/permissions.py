"""Capability-based tool access (AgentOS.pdf p.7)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class PermissionDenied(Exception):
    """An agent requested a capability it does not hold."""


class Permissions:
    """The p.7 permission matrix, optionally backed by a watched JSON file."""

    def __init__(
        self,
        grants: dict[str, list[str]] | None = None,
        path: Path | str | None = None,
    ) -> None:
        self.grants: dict[str, set[str]] = {
            agent: set(caps) for agent, caps in (grants or {}).items()
        }
        self.pid_grants: dict[int, set[str]] = {}
        self.path = Path(path) if path is not None else None
        self._sig: tuple[int, int] | None = None
        if self.path is not None:
            self.refresh(force=True)

    def assign(self, pid: int, capabilities: set[str] | list[str]) -> None:
        """Pin an exact capability set to one process."""
        self.pid_grants[pid] = set(capabilities)

    def forget_process(self, pid: int) -> None:
        self.pid_grants.pop(pid, None)

    @classmethod
    def of(cls, source: Any, default_path: Path) -> "Permissions":
        """What the kernel was configured with, in whatever form it came."""
        if isinstance(source, Permissions):
            return source
        if isinstance(source, dict):
            return cls(grants=source)
        if isinstance(source, (str, Path)):
            return cls(path=source)
        return cls(path=default_path)

    def capabilities(self, agent: str, pid: int | None = None) -> set[str]:
        """Everything this process may reach."""
        if pid is not None and pid in self.pid_grants:
            return set(self.pid_grants[pid])
        return set(self.grants.get(agent, ())) | set(self.grants.get("*", ()))

    def allowed(self, agent: str, capability: str, pid: int | None = None) -> bool:
        if pid is not None and pid in self.pid_grants:
            caps = self.pid_grants[pid]
            return capability in caps or "*" in caps
        for scope in (agent, "*"):
            caps = self.grants.get(scope, ())
            if capability in caps or "*" in caps:
                return True
        return False

    def _signature(self) -> tuple[int, int] | None:
        try:
            stat = self.path.stat()
            return (stat.st_mtime_ns, stat.st_size)
        except OSError:
            return None

    def refresh(self, force: bool = False) -> None:
        """Re-read the matrix if the file changed since we last looked."""
        if self.path is None:
            return
        sig = self._signature()
        if not force and sig == self._sig:
            return
        self._sig = sig
        if sig is None:
            self.grants = {}
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.grants = {agent: set(caps) for agent, caps in data.items()}
        except (ValueError, AttributeError, TypeError):
            pass  # a half-saved or malformed file keeps the previous matrix

    def grant(self, agent: str, capability: str) -> None:
        self.grants.setdefault(agent, set()).add(capability)
        self._save()

    def revoke(self, agent: str, capability: str) -> None:
        caps = self.grants.get(agent)
        if caps is not None:
            caps.discard(capability)
            if not caps:
                del self.grants[agent]
        self._save()

    def _save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(
                {agent: sorted(caps) for agent, caps in sorted(self.grants.items())},
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        self._sig = self._signature()
