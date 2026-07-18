"""Capability-based tool access (AgentOS.pdf p.7).

The permission matrix maps an agent's *name* to the capabilities it may
request. Deny is the default: an agent holds nothing it was not granted, and
the kernel checks the matrix before dispatch — the application does not get a
vote.

The matrix lives in a JSON file (`.agentos/permissions.json` by default) so
that granting or revoking is a config change, not a code change. The kernel
re-reads the file when it changes, which means revocation applies to a running
system: the next request_tool() after the edit is refused.

    {
      "Finance": ["sql"],
      "Coder":   ["filesystem", "python"],
      "*":       []
    }

`"*"` as an agent name grants to every agent; `"*"` as a capability grants
every capability. Both exist for tests and toy runs, not for production humility.
"""

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
        self.path = Path(path) if path is not None else None
        self._sig: tuple[int, int] | None = None
        if self.path is not None:
            self.refresh(force=True)

    @classmethod
    def of(cls, source: Any, default_path: Path) -> "Permissions":
        """What the kernel was configured with, in whatever form it came."""
        if isinstance(source, Permissions):
            return source
        if isinstance(source, dict):
            return cls(grants=source)
        if isinstance(source, (str, Path)):
            return cls(path=source)
        return cls(path=default_path)  # None: watch the standard location

    # -- the check the kernel makes ---------------------------------------
    def capabilities(self, agent: str) -> set[str]:
        """Everything this agent name holds, its own grants plus "*"'s.

        The p.3 process card shows an agent's permissions alongside its PID
        and status, so the kernel needs the whole set and not just a yes/no.
        """
        return set(self.grants.get(agent, ())) | set(self.grants.get("*", ()))

    def allowed(self, agent: str, capability: str) -> bool:
        for scope in (agent, "*"):
            caps = self.grants.get(scope, ())
            if capability in caps or "*" in caps:
                return True
        return False

    # -- the file the humans edit ------------------------------------------
    def _signature(self) -> tuple[int, int] | None:
        try:
            stat = self.path.stat()
            return (stat.st_mtime_ns, stat.st_size)
        except OSError:
            return None  # no file: nothing is granted

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
