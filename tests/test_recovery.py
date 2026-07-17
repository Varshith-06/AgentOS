"""Phase 6: checkpoints and crash recovery.

The bar (AgentOS.pdf p.17): kill the runtime mid-execution, restart it, and
agents resume from their last checkpoint instead of re-running from scratch.
A hard kill costs the work since the last completed syscall and nothing more.

The crash is simulated the way kill -9 behaves: every task dies instantly and
nothing gets to clean up. The store keeps only what was already persisted —
which, because every syscall reply is journaled, is everything that matters.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agentos import Agent, Kernel  # noqa: E402
from agentos.kernel.memory import MemoryManager  # noqa: E402
from agentos.kernel.states import AgentState  # noqa: E402
from agentos.kernel.store import Store  # noqa: E402


class Journaled(Agent):
    """Does one irreversible thing, naps, then checks it happened once."""

    async def run(self, ctx):
        before = (await ctx.memory.retrieve("count", kind="longterm")) or 0
        await ctx.memory.store("count", before + 1, kind="longterm")
        await ctx.sleep(0.4)  # the crash lands here
        after = await ctx.memory.retrieve("count", kind="longterm")
        return {"before": before, "after": after}


class QuickChild(Agent):
    async def run(self, ctx):
        n = (await ctx.memory.retrieve("quick_runs", kind="longterm")) or 0
        await ctx.memory.store("quick_runs", n + 1, kind="longterm")
        return {"ran": n + 1}


class SlowChild(Agent):
    async def run(self, ctx):
        await ctx.sleep(0.5)
        return "slow done"


class Parent(Agent):
    async def run(self, ctx):
        quick = await ctx.spawn(QuickChild())
        slow = await ctx.spawn(SlowChild())
        result = await ctx.wait_all(agents=[quick, slow])
        return {str(pid): r for pid, r in result["agents"].items()}


class ToolWorker(Agent):
    async def run(self, ctx):
        out = await ctx.request_tool(
            "python",
            "run",
            code=f"open({self.params['path']!r}, 'a').write('ran\\n'); print('done')",
        )
        await ctx.sleep(0.5)  # crash after the tool completed
        return out["stdout"].strip()


class Gated(Agent):
    async def run(self, ctx):
        approval = await ctx.request_approval(role="Operator", reason="resume test")
        return approval["by"]


class Announcer(Agent):
    async def run(self, ctx):
        await ctx.sleep(0.1)
        await ctx.publish("BigNews", detail="it works")
        return "published"


class Listener(Agent):
    async def run(self, ctx):
        await ctx.subscribe("BigNews")
        await ctx.sleep(0.4)  # busy while the news lands; crash here
        event = await ctx.wait_event("BigNews")
        return event["detail"]


class BadReturn(Agent):
    async def run(self, ctx):
        return object()  # cannot cross the boundary, cannot be recovered


class RecoveryTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def kernel(self, **kw):
        return Kernel(store=self.store, tick=0.01, **kw)

    async def _until(self, predicate, timeout=5.0):
        async def poll():
            while not predicate():
                await asyncio.sleep(0.01)

        await asyncio.wait_for(poll(), timeout)

    async def _crash(self, k, run_task):
        """kill -9, in-process: every task dies instantly, nothing cleans up.

        The kernel's store is swapped for a scratch one first, so the death
        throes (task cancellations firing _on_fail) cannot touch the real
        persisted state — exactly like a process that is simply gone.
        """
        scratch_dir = tempfile.TemporaryDirectory()
        self.addCleanup(scratch_dir.cleanup)
        scratch = Store(scratch_dir.name)
        self.addCleanup(scratch.close)
        k.store = scratch
        k.memory = MemoryManager(scratch)

        tasks = [run_task]
        tasks += [p.task for p in k.table.all() if p.task is not None]
        tasks += list(k._io_tasks)
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    # -- the p.17 bar ---------------------------------------------------------
    async def test_a_crashed_agent_resumes_instead_of_rerunning(self):
        k1 = self.kernel()
        pid = k1.spawn(Journaled())
        run1 = asyncio.create_task(k1.run())
        await self._until(lambda: k1.table.get(pid).state is AgentState.SLEEPING)
        await self._crash(k1, run1)

        k2 = self.kernel(recover=True)
        await asyncio.wait_for(k2.run(), timeout=5)
        result = k2.table.get(pid).result
        self.assertEqual(
            result, {"before": 0, "after": 1},
            "the increment must have happened exactly once, and the replayed "
            "retrieve must return what it returned before the crash",
        )
        row = next(r for r in self.store.processes() if r["pid"] == pid)
        self.assertEqual(row["status"], "Finished")
        self.assertGreater(row["checkpoint"], 0, "checkpoints must be visible")

    async def test_finished_children_are_not_rerun(self):
        k1 = self.kernel()
        parent = k1.spawn(Parent())
        run1 = asyncio.create_task(k1.run())
        await self._until(
            lambda: any(
                p.name == "QuickChild" and p.state is AgentState.FINISHED
                for p in k1.table.all()
            )
            and any(
                p.name == "SlowChild" and p.state is AgentState.SLEEPING
                for p in k1.table.all()
            )
        )
        await self._crash(k1, run1)

        k2 = self.kernel(recover=True)
        await asyncio.wait_for(k2.run(), timeout=5)
        results = k2.table.get(parent).result
        self.assertIn({"ran": 1}, results.values())  # the pre-crash result, served
        self.assertIn("slow done", results.values())
        # QuickChild must not have executed a second time:
        counter = self.store.db.execute(
            "SELECT value FROM memory WHERE mtype='longterm' AND owner='QuickChild'"
        ).fetchone()
        self.assertEqual(counter["value"], "1")

    async def test_tool_results_replay_without_rerunning_the_tool(self):
        side_effect = Path(self.tmp.name) / "side_effect.txt"
        k1 = self.kernel(permissions={"ToolWorker": ["python"]})
        pid = k1.spawn(ToolWorker(path=str(side_effect)))
        run1 = asyncio.create_task(k1.run())
        await self._until(lambda: k1.table.get(pid).state is AgentState.SLEEPING,
                          timeout=15)
        await self._crash(k1, run1)

        k2 = self.kernel(recover=True)
        await asyncio.wait_for(k2.run(), timeout=15)
        self.assertEqual(k2.table.get(pid).result, "done")
        self.assertEqual(
            side_effect.read_text().count("ran"), 1,
            "the tool must not run twice — its output came from the journal",
        )

    async def test_a_pending_approval_re_blocks_after_recovery(self):
        k1 = self.kernel()
        pid = k1.spawn(Gated())
        run1 = asyncio.create_task(k1.run())
        await self._until(lambda: k1.table.get(pid).state is AgentState.BLOCKED)
        await self._crash(k1, run1)

        k2 = self.kernel(recover=True)
        run2 = asyncio.create_task(k2.run())
        await self._until(lambda: k2.table.get(pid).state is AgentState.BLOCKED)
        self.assertEqual(
            len(self.store.approvals()), 1,
            "recovery must re-attach to the same approval, not ask twice",
        )
        k2.approve(pid, "Operator")
        await asyncio.wait_for(run2, timeout=5)
        self.assertEqual(k2.table.get(pid).result, "Operator")

    async def test_events_buffered_at_crash_time_are_redelivered(self):
        k1 = self.kernel()
        listener = k1.spawn(Listener())
        k1.spawn(Announcer())
        run1 = asyncio.create_task(k1.run())
        await self._until(
            lambda: any(
                p.name == "Announcer" and p.state is AgentState.FINISHED
                for p in k1.table.all()
            )
        )
        await self._crash(k1, run1)  # the listener never consumed BigNews

        k2 = self.kernel(recover=True)
        await asyncio.wait_for(k2.run(), timeout=5)
        self.assertEqual(k2.table.get(listener).result, "it works")

    async def test_recovering_a_finished_world_is_a_no_op(self):
        k1 = self.kernel()
        await asyncio.wait_for(k1.run_until_done(QuickChild()), timeout=5)

        k2 = self.kernel(recover=True)
        await asyncio.wait_for(k2.run(), timeout=5)  # returns immediately
        self.assertEqual(len(k2.table.all()), 1)
        self.assertIs(k2.table.all()[0].state, AgentState.FINISHED)

    # -- the discipline that makes all of this possible -----------------------
    async def test_a_non_serializable_result_fails_the_agent(self):
        k = self.kernel()
        pid = k.spawn(BadReturn())
        await asyncio.wait_for(k.run(), timeout=5)
        proc = k.table.get(pid)
        self.assertIs(proc.state, AgentState.FAILED)
        self.assertIn("serializable", proc.exit_reason)


if __name__ == "__main__":
    unittest.main(verbosity=2)
