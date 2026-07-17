"""The python driver: run code in a fresh interpreter, never in the kernel's."""

from __future__ import annotations

import asyncio
import sys
from typing import Any

from .base import ToolDriver


class Python(ToolDriver):
    name = "python"
    timeout = 30.0

    async def op_run(self, code: str) -> dict[str, Any]:
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            code,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await proc.communicate()
        except asyncio.CancelledError:
            proc.kill()
            raise
        return {
            "stdout": out.decode(errors="replace"),
            "stderr": err.decode(errors="replace"),
            "returncode": proc.returncode,
        }
