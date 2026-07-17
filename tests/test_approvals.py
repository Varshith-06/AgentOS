"""Phase 3: human approval as a first-class kernel object.

The bar (AgentOS.pdf p.15): a pending approval must survive a runtime restart.
A human dependency that evaporates on restart is not a kernel object — it is a
callback with good PR. These tests kill the runtime mid-block and check that
the approval, and even a grant issued while nothing was running, is honored.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agentos import Agent, Kernel  # noqa: E402
from agentos.kernel.states import AgentState  # noqa: E402
from agentos.kernel.store import Store  # noqa: E402


class Deploy(Agent):
    async def run(self, ctx):
        approval = await ctx.request_approval(
            role=self.params.get("role", "Senior Engineer"),
            reason=self.params.get("reason", "production deployment"),
        )
        return {"deployed": True, "by": approval["by"]}


class EventWatcher(Agent):
    """Approvals are events too: HumanApproved wakes ordinary subscribers."""

    async def run(self, ctx):
        event = await ctx.wait_event("HumanApproved")
        return event["role"]


class ApprovalTest(unittest.IsolatedAsyncioTestCase):
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

    async def _blocked(self, k, pid):
        await self._until(lambda: k.table.get(pid).state is AgentState.BLOCKED)

    async def _crash(self, k, run_task):
        """The runtime dies mid-run: every task stops, the store remains."""
        run_task.cancel()
        tasks = [p.task for p in k.table.all() if p.task is not None]
        for t in tasks:
            t.cancel()
        await asyncio.gather(run_task, *tasks, return_exceptions=True)

    # -- blocking and resuming (p.6) ---------------------------------------
    async def test_request_approval_blocks_until_a_human_approves(self):
        k = self.kernel()
        pid = k.spawn(Deploy())
        run = asyncio.create_task(k.run())
        await self._blocked(k, pid)
        self.assertEqual(k.table.get(pid).waiting_on, "Senior Engineer")

        # Blocked on a human is not a deadlock: the runtime must keep serving.
        await asyncio.sleep(0.1)
        self.assertFalse(run.done())
        self.assertIs(k.table.get(pid).state, AgentState.BLOCKED)

        k.approve(pid, "Senior Engineer")
        await asyncio.wait_for(run, timeout=5)
        proc = k.table.get(pid)
        self.assertIs(proc.state, AgentState.FINISHED)
        self.assertEqual(proc.result, {"deployed": True, "by": "Senior Engineer"})

    async def test_the_wrong_role_cannot_approve(self):
        k = self.kernel()
        pid = k.spawn(Deploy())
        run = asyncio.create_task(k.run())
        await self._blocked(k, pid)

        with self.assertRaises(ValueError):
            k.approve(pid, "Intern")
        self.assertIs(k.table.get(pid).state, AgentState.BLOCKED)

        k.approve(pid, "Senior Engineer")
        await asyncio.wait_for(run, timeout=5)
        self.assertIs(k.table.get(pid).state, AgentState.FINISHED)

    async def test_each_live_request_needs_its_own_grant(self):
        """Two identical requests are two approvals, not a shared shortcut."""
        k = self.kernel()
        a = k.spawn(Deploy())
        b = k.spawn(Deploy())
        run = asyncio.create_task(k.run())
        await self._blocked(k, a)
        await self._blocked(k, b)

        k.approve(a, "Senior Engineer")
        await self._until(lambda: k.table.get(a).state is AgentState.FINISHED)
        self.assertIs(k.table.get(b).state, AgentState.BLOCKED)

        k.approve(b, "Senior Engineer")
        await asyncio.wait_for(run, timeout=5)

    async def test_approval_publishes_a_human_approved_event(self):
        k = self.kernel()
        watcher = k.spawn(EventWatcher())
        pid = k.spawn(Deploy())
        run = asyncio.create_task(k.run())
        await self._blocked(k, pid)
        k.approve(pid, "Senior Engineer")
        await asyncio.wait_for(run, timeout=5)
        self.assertEqual(k.table.get(watcher).result, "Senior Engineer")

    async def test_killing_a_blocked_agent_does_not_hang(self):
        k = self.kernel()
        pid = k.spawn(Deploy())
        run = asyncio.create_task(k.run())
        await self._blocked(k, pid)
        k.kill(pid)
        await asyncio.wait_for(run, timeout=5)
        proc = k.table.get(pid)
        self.assertIs(proc.state, AgentState.FAILED)
        self.assertIn("killed", proc.exit_reason)

    # -- the restart bar (p.15: "done when") --------------------------------
    async def test_pending_approval_survives_a_runtime_restart(self):
        k1 = self.kernel()
        pid1 = k1.spawn(Deploy())
        run1 = asyncio.create_task(k1.run())
        await self._blocked(k1, pid1)
        original = self.store.approvals()[0]
        await self._crash(k1, run1)

        k2 = self.kernel()  # a restart wipes processes; approvals survive
        pending = self.store.approvals()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["status"], "pending")
        self.assertIsNone(pending[0]["pid"])  # old pids mean nothing now

        pid2 = k2.spawn(Deploy())
        run2 = asyncio.create_task(k2.run())
        await self._blocked(k2, pid2)
        adopted = self.store.approvals()
        self.assertEqual(len(adopted), 1, "must re-attach, not ask the human twice")
        self.assertEqual(adopted[0]["id"], original["id"])

        k2.approve(pid2, "Senior Engineer")
        await asyncio.wait_for(run2, timeout=5)
        self.assertEqual(k2.table.get(pid2).result["deployed"], True)

    async def test_a_grant_issued_while_down_is_honored_on_restart(self):
        k1 = self.kernel()
        pid1 = k1.spawn(Deploy())
        run1 = asyncio.create_task(k1.run())
        await self._blocked(k1, pid1)
        await self._crash(k1, run1)

        self.store.approve(pid1, "Senior Engineer")  # the CLI path: runtime is dead

        k2 = self.kernel()
        result = await asyncio.wait_for(k2.run_until_done(Deploy()), timeout=5)
        self.assertEqual(result, {"deployed": True, "by": "Senior Engineer"})
        self.assertEqual(self.store.approvals(), [])  # granted, consumed, done

    # -- suspension interplay ------------------------------------------------
    async def test_approval_while_suspended_is_delivered_on_resume(self):
        k = self.kernel()
        pid = k.spawn(Deploy())
        run = asyncio.create_task(k.run())
        await self._blocked(k, pid)

        k.pause(pid)
        self.assertIs(k.table.get(pid).state, AgentState.SUSPENDED)
        k.approve(pid, "Senior Engineer")
        await asyncio.sleep(0.05)  # ticks pass; a suspended agent stays suspended
        self.assertIs(k.table.get(pid).state, AgentState.SUSPENDED)

        k.resume(pid)
        await asyncio.wait_for(run, timeout=5)
        self.assertIs(k.table.get(pid).state, AgentState.FINISHED)

    async def test_resume_before_approval_keeps_it_suspended(self):
        """Waking an agent whose wait has not resolved would hand it nothing."""
        k = self.kernel()
        pid = k.spawn(Deploy())
        run = asyncio.create_task(k.run())
        await self._blocked(k, pid)

        k.pause(pid)
        k.resume(pid)  # nothing to deliver yet: it must stay suspended
        self.assertIs(k.table.get(pid).state, AgentState.SUSPENDED)

        k.approve(pid, "Senior Engineer")
        await asyncio.sleep(0.05)
        k.resume(pid)
        await asyncio.wait_for(run, timeout=5)
        self.assertIs(k.table.get(pid).state, AgentState.FINISHED)


if __name__ == "__main__":
    unittest.main(verbosity=2)
