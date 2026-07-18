"""The filesystem driver: file access confined to a sandbox root."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .base import ToolDriver, ToolError


class Filesystem(ToolDriver):
    name = "filesystem"
    timeout = 10.0
    cacheable = ("read", "list", "exists")  # writes are never served stale

    def __init__(self, root: str | Path = ".", **kw: Any) -> None:
        super().__init__(**kw)
        self.root = Path(root).resolve()

    def _resolve(self, path: str) -> Path:
        """Every path is interpreted inside the sandbox, or refused."""
        p = (self.root / path).resolve()
        if p != self.root and self.root not in p.parents:
            raise ToolError(f"path escapes the sandbox root: {path!r}")
        return p

    async def op_read(self, path: str) -> str:
        return self._resolve(path).read_text(encoding="utf-8")

    async def op_write(self, path: str, content: str) -> dict[str, Any]:
        p = self._resolve(path)
        existed = p.exists()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        if not existed:
            self._publish("FileCreated", path=str(p))  # p.5 kernel event
        return {"path": str(p), "bytes": len(content.encode("utf-8"))}

    async def op_list(self, path: str = ".") -> list[str]:
        return sorted(child.name for child in self._resolve(path).iterdir())

    async def op_exists(self, path: str) -> bool:
        return self._resolve(path).exists()
