"""Phase 1 kernel tests. Stdlib only: python -m unittest discover tests -v"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agentos import Agent, Kernel  # noqa: E402
from agentos.kernel.messages import NotSerializable  # noqa: E402
from agentos.kernel.process import ProcessTable  # noqa: E402
from agentos.kernel.states import AgentState, InvalidTransition  # noqa: E402
from agentos.kernel.store import Store  # noqa: E402


class Sleeper(Agent):
    async def run(self, ctx):
        await ctx.sleep(self.params.get("duration", 0.05))
        return self.params.get("value", "done")


class Exploder(Agent):
    async def run(self, ctx):
        raise ValueError("agent bug")


class Parent(Agent):
    async def run(self, ctx):
        pid = await ctx.spawn(Sleeper(value="from child"))
        return await ctx.wait(pid)


class Immortal(Agent):
    async def run(self, ctx):
        await ctx.sleep(30)
        return "never"


class Nester(Agent):
    """parent -> child -> grandchild, then sleeps forever."""

    async def run(self, ctx):
        depth = self.params.get("depth", 0)
        if depth < 2:
            await ctx.spawn(Nester(depth=depth + 1))
        await ctx.sleep(30)


class KernelTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def kernel(self, **kw):
        return Kernel(store=self.store, tick=0.01, **kw)

    # -- lifecycle -------------------------------------------------------
    def test_illegal_transition_raises(self):
        table = ProcessTable()
        proc = table.create("X", {})
        table.transition(proc, AgentState.RUNNING)
        table.transition(proc, AgentState.FINISHED)
        with self.assertRaises(InvalidTransition):
            table.transition(proc, AgentState.RUNNING)  # terminal is terminal

    def test_ready_cannot_skip_to_waiting(self):
        table = ProcessTable()
        proc = table.create("X", {})
        with self.assertRaises(InvalidTransition):
            table.transition(proc, AgentState.WAITING)

    async def test_agent_runs_and_finishes(self):
        k = self.kernel()
        result = await k.run_until_done(Sleeper(value=42))
        self.assertEqual(result, 42)
        self.assertIs(k.table.get(1).state, AgentState.FINISHED)

    async def test_agent_exception_fails_process_not_kernel(self):
        k = self.kernel()
        pid = k.spawn(Exploder())
        k.spawn(Sleeper(value="survivor"))
        await k.run()
        self.assertIs(k.table.get(pid).state, AgentState.FAILED)
        self.assertIn("ValueError", k.table.get(pid).exit_reason)
        self.assertIs(k.table.get(2).state, AgentState.FINISHED)  # kernel survived

    # -- tree and wait ---------------------------------------------------
    async def test_spawn_builds_parent_child_links(self):
        k = self.kernel()
        result = await k.run_until_done(Parent())
        self.assertEqual(result, "from child")
        self.assertEqual(k.table.get(1).children, [2])
        self.assertEqual(k.table.get(2).parent, 1)

    async def test_wait_blocks_then_returns_child_result(self):
        k = self.kernel()
        await k.run_until_done(Parent())
        states = [
            e["message"]
            for e in self.store.logs(pid=1)
            if e["kind"] == "state"
        ]
        self.assertIn("Running -> Waiting", states)
        self.assertIn("Waiting -> Ready", states)

    # -- scheduling ------------------------------------------------------
    async def test_slots_are_never_oversubscribed(self):
        k = self.kernel(slots=2)
        for i in range(6):
            k.spawn(Sleeper(duration=0.05, value=i))

        peak = 0

        async def watch():
            nonlocal peak
            while not k._quiescent():
                peak = max(peak, len(k.running))
                await asyncio.sleep(0.005)

        await asyncio.gather(k.run(), watch())
        self.assertLessEqual(peak, 2)
        self.assertEqual(peak, 2)  # and it does actually use both

    async def test_woken_agent_requeues_instead_of_resuming_instantly(self):
        """A scheduler, not a callback: Sleeping -> Ready -> Running."""
        k = self.kernel(slots=1)
        await k.run_until_done(Sleeper(duration=0.02))
        states = [e["message"] for e in self.store.logs(pid=1) if e["kind"] == "state"]
        self.assertEqual(
            states,
            [
                "Ready -> Running",
                "Running -> Sleeping",
                "Sleeping -> Ready",
                "Ready -> Running",
                "Running -> Finished",
            ],
        )

    # -- control ---------------------------------------------------------
    async def test_kill_child_leaves_parent_alive(self):
        k = self.kernel()
        parent = k.spawn(Immortal())
        child = k.spawn(Immortal(), parent=parent)
        runner = asyncio.create_task(k.run())
        await asyncio.sleep(0.05)

        k.kill(child)
        await asyncio.sleep(0.05)
        self.assertIs(k.table.get(child).state, AgentState.FAILED)
        self.assertEqual(k.table.get(child).exit_reason, "killed")
        self.assertTrue(k.table.get(parent).alive)

        k.kill(parent)
        await runner

    async def test_kill_cascades_to_descendants_only(self):
        k = self.kernel()
        root = k.spawn(Nester(depth=0))
        runner = asyncio.create_task(k.run())
        await asyncio.sleep(0.1)
        self.assertEqual(len(k.table.all()), 3)  # root -> child -> grandchild

        k.kill(2)  # the middle one
        await asyncio.sleep(0.05)
        self.assertTrue(k.table.get(1).alive)  # ancestor untouched
        self.assertIs(k.table.get(2).state, AgentState.FAILED)
        self.assertIs(k.table.get(3).state, AgentState.FAILED)  # descendant taken

        k.kill(1)
        await runner

    async def test_pause_and_resume_via_control_queue(self):
        """The CLI path: commands arrive through the store, not the API."""
        k = self.kernel(slots=1)
        pid = k.spawn(Sleeper(duration=0.05))
        runner = asyncio.create_task(k.run())

        self.store.send_command("pause", pid=pid)
        await asyncio.sleep(0.15)
        self.assertIs(k.table.get(pid).state, AgentState.SUSPENDED)

        self.store.send_command("resume", pid=pid)
        await asyncio.wait_for(runner, timeout=2)
        self.assertIs(k.table.get(pid).state, AgentState.FINISHED)

    # -- the message boundary --------------------------------------------
    def test_non_serializable_params_are_rejected_at_construction(self):
        with self.assertRaises(NotSerializable):
            Sleeper(callback=lambda: None)  # cannot survive a pipe -> refused

    async def test_agent_context_exposes_no_kernel_handle(self):
        """An agent must not be able to reach the kernel or another agent.

        This deliberately pins the whole surface rather than spot-checking it:
        every future phase adds syscalls, and each one should have to justify
        itself by failing this test on the way in.
        """
        from agentos.runtime.executor import Context

        surface = {a for a in dir(Context) if not a.startswith("_")}
        self.assertEqual(
            surface,
            {
                # Phase 1: processes
                "pid", "name", "spawn", "sleep", "wait", "log",
                # Phase 2: events and the dependency graph
                "publish", "subscribe", "wait_event", "wait_all",
                # Phase 3: a human is a dependency like any other
                "request_approval",
                # Phase 4: capabilities, never libraries
                "request_tool",
                # Phase 5: memory as a kernel service; models by class, not name
                "memory", "request_model",
            },
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
