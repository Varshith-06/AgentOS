"""The shell driver: run a command, bound its runtime, report all three outputs."""

from __future__ import annotations

import asyncio
from typing import Any

from .base import ToolDriver


class Shell(ToolDriver):
    name = "shell"
    timeout = 30.0

    async def op_run(self, command: str, cwd: str | None = None) -> dict[str, Any]:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
        )
        try:
            out, err = await proc.communicate()
        except asyncio.CancelledError:
            proc.kill()  # the timeout in execute() cancelled us: take the child too
            raise
        return {
            "stdout": out.decode(errors="replace"),
            "stderr": err.decode(errors="replace"),
            "returncode": proc.returncode,
        }
