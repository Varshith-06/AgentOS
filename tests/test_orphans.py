"""A runtime killed hard must not leave its agents running without it."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agentos import Agent  # noqa: E402

from tests._timing import LIMIT  # noqa: E402


class Napper(Agent):
    """Writes its OS pid, then waits on a reply that will never come."""

    async def run(self, ctx):
        Path(self.params["pidfile"]).write_text(str(os.getpid()))
        await ctx.sleep(600)
        return "woke"


KERNEL = textwrap.dedent("""
    import asyncio, sys
    sys.path.insert(0, {root!r})
    from agentos import Kernel
    from agentos.kernel.store import Store
    from tests.test_orphans import Napper
    k = Kernel(store=Store({store!r}), tick=0.01)
    k.spawn(Napper(pidfile={pidfile!r}))
    asyncio.run(k.run())
""")


def alive(pid: int) -> bool:
    if os.name == "nt":
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True
        ).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


class OrphanTest(unittest.TestCase):
    def test_agents_exit_when_their_runtime_is_killed(self):
        with tempfile.TemporaryDirectory() as tmp:
            pidfile = Path(tmp) / "agent.pid"
            script = KERNEL.format(
                root=str(ROOT), store=str(Path(tmp) / "store"), pidfile=str(pidfile)
            )
            kernel = subprocess.Popen(
                [sys.executable, "-X", "utf8", "-c", script],
                cwd=ROOT, env=dict(os.environ, PYTHONPATH=str(ROOT)),
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            try:
                deadline = time.time() + LIMIT
                while not pidfile.exists() or not pidfile.read_text():
                    self.assertLess(time.time(), deadline, "agent never started")
                    time.sleep(0.05)
                agent = int(pidfile.read_text())
                time.sleep(0.3)  # let the agent settle into its wait
            finally:
                kernel.kill()  # SIGKILL / TerminateProcess: no cleanup runs
                kernel.wait()

            deadline = time.time() + 10
            while alive(agent) and time.time() < deadline:
                time.sleep(0.1)
            if alive(agent):
                if os.name == "nt":
                    subprocess.run(["taskkill", "/F", "/PID", str(agent)],
                                   capture_output=True)
                else:
                    os.kill(agent, 9)
                self.fail("the agent outlived its runtime")


if __name__ == "__main__":
    unittest.main(verbosity=2)
