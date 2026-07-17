"""Crash recovery (AgentOS.pdf p.7-8) — the demo that sells the project.

Three workers each grind through five slow steps. Every step is real work
with a visible side effect: a line appended to .agentos/crash_log.txt through
the python tool driver. The kernel journals every syscall reply, so every
completed step is a checkpoint.

The demo:

    python -m agentos.cli run examples/crash.py     # note the os_pid it prints
    # mid-run, from another terminal, kill it dead:
    #   taskkill /F /PID <os_pid>        (Windows)
    #   kill -9 <os_pid>                 (elsewhere)
    python -m agentos.cli recover

Recovery re-creates each worker from its spec and replays its journal:
completed steps return their recorded results instantly — the tool does NOT
run again — and the worker goes live exactly where it died. When it is done,
count the lines:

    every (worker, step) pair appears exactly once.

A hard kill cost the work since the last completed syscall, and nothing more.
Watch it happen: `python -m agentos.cli top` during either run, and
`agent logs | grep recover` afterwards.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from agentos import Agent, Kernel, Permissions

PERMISSIONS = Path(".agentos/permissions.json")
CRASH_LOG = Path(".agentos/crash_log.txt")


class Worker(Agent):
    async def run(self, ctx):
        tag = self.params["tag"]
        for step in range(1, 6):
            code = (
                "from pathlib import Path\n"
                f"p = Path({str(CRASH_LOG.resolve())!r})\n"
                f"p.open('a').write('{tag} step {step}\\n')\n"
                f"print('{tag} step {step} committed')"
            )
            out = await ctx.request_tool("python", "run", code=code)
            await ctx.log(out["stdout"].strip())
            await ctx.sleep(1.0)  # slow enough to be killed mid-run
        return {"worker": tag, "steps": 5}


async def main(slots: int = 4, policy: str = "fifo") -> int:
    perms = Permissions(path=PERMISSIONS)
    if not perms.allowed("Worker", "python"):
        perms.grant("Worker", "python")
    CRASH_LOG.unlink(missing_ok=True)

    kernel = Kernel(policy=policy, slots=slots)
    for tag in ("alpha", "beta", "gamma"):
        kernel.spawn(Worker(tag=tag))

    print(f"3 workers x 5 slow steps. os_pid={os.getpid()}")
    print("kill this process mid-run, then: python -m agentos.cli recover\n")
    await kernel.run()

    lines = CRASH_LOG.read_text().splitlines() if CRASH_LOG.exists() else []
    print(f"\ncrash_log.txt has {len(lines)} lines; "
          f"{len(set(lines))} unique - no step ran twice" if lines else "no steps ran")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
