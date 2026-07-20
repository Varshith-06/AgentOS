"""The design-doc items that were specified but unimplemented until now.

Each test here names the page of AgentOS.pdf it is defending, because that is
the only reason these features exist. An audit found them missing; a test is
what stops them going missing again.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agentos import Agent, Kernel  # noqa: E402
from agentos.drivers.base import ToolDriver, Transient  # noqa: E402
from agentos.kernel import gpu  # noqa: E402
from agentos.kernel.models import ModelManager  # noqa: E402
from agentos.kernel.states import AgentState  # noqa: E402
from agentos.kernel.store import Store  # noqa: E402


class Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(self.tmp.name)
        self.addCleanup(self.store.close)

    def kernel(self, **kw):
        kw.setdefault("tick", 0.01)
        return Kernel(store=self.store, **kw)


# -- p.9: kernel.checkpoint(), and the p.3 Checkpointing state ---------------

class Checkpointer(Agent):
    async def run(self, ctx):
        await ctx.log("before")
        n = await ctx.checkpoint("halfway")
        return {"checkpoint": n}


class CheckpointTest(Base):
    async def test_explicit_checkpoint_returns_its_number_and_is_journaled(self):
        result = await asyncio.wait_for(
            self.kernel().run_until_done(Checkpointer()), timeout=30)
        self.assertGreater(result["checkpoint"], 0)
        ops = [e["op"] for entries in self.store.load_journals().values()
               for e in entries]
        self.assertIn("checkpoint", ops)

    async def test_checkpointing_state_is_actually_entered(self):
        """p.3 lists Checkpointing as a lifecycle state. Before this it was
        declared in the enum and never reached by any code path."""
        k = self.kernel()
        seen: list[str] = []
        original = k.table.on_transition

        def spy(proc, frm=None, to=None):
            seen.append(proc.state.value)
            if original:
                original(proc, frm, to)

        k.table.on_transition = spy
        await asyncio.wait_for(k.run_until_done(Checkpointer()), timeout=30)
        self.assertIn(AgentState.CHECKPOINTING.value, seen)


# -- p.3: the process card carries Model and Permissions --------------------

class Idle(Agent):
    async def run(self, ctx):
        await ctx.sleep(0.01)
        return "ok"


class Napper(Agent):
    """Module level, like every agent must be: a child process re-creates it
    from its spec by importing it, and a class defined inside a test method
    has no importable name."""

    async def run(self, ctx):
        await ctx.sleep(self.params.get("nap", 60))


class ModelCaller(Agent):
    async def run(self, ctx):
        await ctx.request_model(self.params.get("need", "fast"), prompt="hello")
        return "done"


class ProcessCardTest(Base):
    async def test_permissions_appear_on_the_process(self):
        k = self.kernel(permissions={"Idle": ["sql", "http"]})
        pid = k.spawn(Idle())
        row = k.table.get(pid).row()
        self.assertEqual(sorted(row["permissions"]), ["http", "sql"])

    async def test_model_is_recorded_on_the_process(self):
        k = self.kernel(models={"classes": {"fast": [
            {"provider": "mock", "model": "mock-fast", "cost_per_mtok": [1, 1]}]}})

        pid = k.spawn(ModelCaller())
        await asyncio.wait_for(k.run(), timeout=30)
        self.assertEqual(k.table.get(pid).row()["model"], "mock-fast")

    async def test_row_stays_serializable(self):
        from agentos.kernel.messages import assert_serializable
        k = self.kernel(permissions={"*": ["sql"]})
        pid = k.spawn(Idle())
        assert_serializable("row", k.table.get(pid).row())


# -- p.4: retries are a scheduler responsibility ----------------------------

class FlakyAgent(Agent):
    """Fails the first N times it is started, then succeeds.

    The attempt counter is a file rather than class state, because a restart
    is a brand-new OS process: anything held in memory died with the attempt
    that failed. That is the point of the retry — and the reason a counter
    has to live somewhere both processes can see.
    """

    async def run(self, ctx):
        counter = Path(self.params["counter"])
        n = int(counter.read_text()) + 1 if counter.exists() else 1
        counter.write_text(str(n))
        if n <= self.params["fail_times"]:
            raise RuntimeError(f"attempt {n} fails on purpose")
        return {"attempts": n}


class RetryTest(Base):
    def counter(self, tag: str) -> str:
        return str(Path(self.tmp.name) / f"attempts-{tag}.txt")

    async def test_a_failing_agent_is_restarted_up_to_its_budget(self):
        k = self.kernel(retries=2)
        pid = k.spawn(FlakyAgent(counter=self.counter("a"), fail_times=1))
        await asyncio.wait_for(k.run(), timeout=30)
        proc = k.table.get(pid)
        self.assertEqual(proc.state, AgentState.FINISHED)
        self.assertEqual(proc.retries, 1)

    async def test_retries_are_bounded(self):
        k = self.kernel(retries=1)
        pid = k.spawn(FlakyAgent(counter=self.counter("b"), fail_times=99))
        await asyncio.wait_for(k.run(), timeout=30)
        proc = k.table.get(pid)
        self.assertEqual(proc.state, AgentState.FAILED)
        self.assertEqual(proc.retries, 1)  # one restart, then give up

    async def test_retries_are_off_by_default(self):
        k = self.kernel()
        pid = k.spawn(FlakyAgent(counter=self.counter("c"), fail_times=1))
        await asyncio.wait_for(k.run(), timeout=30)
        self.assertEqual(k.table.get(pid).state, AgentState.FAILED)

    async def test_a_killed_agent_is_never_retried(self):
        """A human said stop. Restarting would be the kernel overruling them."""
        k = self.kernel(retries=5)
        pid = k.spawn(Napper(nap=60))
        run = asyncio.create_task(k.run())
        while k.table.get(pid).state is not AgentState.SLEEPING:
            await asyncio.sleep(0.01)
        k.kill(pid)
        await asyncio.wait_for(run, timeout=30)
        proc = k.table.get(pid)
        self.assertEqual(proc.state, AgentState.FAILED)
        self.assertEqual(proc.retries, 0)


# -- p.7: caching is a driver responsibility --------------------------------

class Counter(ToolDriver):
    name = "counter"
    cacheable = ("read",)

    def __init__(self, **kw):
        super().__init__(**kw)
        self.calls = 0

    async def op_read(self, key: str) -> int:
        self.calls += 1
        return self.calls

    async def op_write(self, key: str) -> int:
        self.calls += 1
        return self.calls


class DriverCacheTest(unittest.IsolatedAsyncioTestCase):
    async def test_a_cacheable_op_runs_once_within_its_ttl(self):
        d = Counter(cache_ttl=60)
        self.assertEqual(await d.execute("read", {"key": "a"}), 1)
        self.assertEqual(await d.execute("read", {"key": "a"}), 1)
        self.assertEqual(d.calls, 1)

    async def test_different_params_are_different_entries(self):
        d = Counter(cache_ttl=60)
        await d.execute("read", {"key": "a"})
        await d.execute("read", {"key": "b"})
        self.assertEqual(d.calls, 2)

    async def test_writes_are_never_cached(self):
        d = Counter(cache_ttl=60)
        await d.execute("write", {"key": "a"})
        await d.execute("write", {"key": "a"})
        self.assertEqual(d.calls, 2)

    async def test_caching_is_off_unless_a_ttl_is_configured(self):
        d = Counter()
        await d.execute("read", {"key": "a"})
        await d.execute("read", {"key": "a"})
        self.assertEqual(d.calls, 2)

    async def test_an_expired_entry_is_refetched(self):
        d = Counter(cache_ttl=0.05)
        await d.execute("read", {"key": "a"})
        await asyncio.sleep(0.08)
        await d.execute("read", {"key": "a"})
        self.assertEqual(d.calls, 2)


# -- p.7: model selection criteria, not just config order -------------------

class ModelRankingTest(unittest.IsolatedAsyncioTestCase):
    CANDIDATES = [
        {"provider": "mock", "model": "pricey", "cost_per_mtok": [10, 10],
         "latency": 0.001, "quality": 9},
        {"provider": "mock", "model": "cheap", "cost_per_mtok": [1, 1],
         "latency": 0.05, "quality": 3},
    ]

    async def test_order_is_the_default(self):
        m = ModelManager(classes={"fast": self.CANDIDATES})
        reply = await m.request("fast", prompt="hi")
        self.assertEqual(reply["model"], "pricey")

    async def test_cheapest_wins_when_asked(self):
        m = ModelManager(classes={"fast": {
            "prefer": "cheapest", "candidates": self.CANDIDATES}})
        reply = await m.request("fast", prompt="hi")
        self.assertEqual(reply["model"], "cheap")

    async def test_fastest_wins_when_asked(self):
        m = ModelManager(classes={"fast": {
            "prefer": "fastest", "candidates": self.CANDIDATES}})
        reply = await m.request("fast", prompt="hi")
        self.assertEqual(reply["model"], "pricey")

    async def test_best_wins_when_asked(self):
        m = ModelManager(classes={"fast": {
            "prefer": "best", "candidates": self.CANDIDATES}})
        reply = await m.request("fast", prompt="hi")
        self.assertEqual(reply["model"], "pricey")

    async def test_a_candidate_that_cannot_fit_the_prompt_sorts_last(self):
        m = ModelManager(classes={"fast": {"prefer": "cheapest", "candidates": [
            {"provider": "mock", "model": "tiny", "cost_per_mtok": [0, 0],
             "context_window": 1},
            {"provider": "mock", "model": "roomy", "cost_per_mtok": [5, 5]},
        ]}})
        reply = await m.request("fast", prompt="a much longer prompt " * 20)
        self.assertEqual(reply["model"], "roomy")


# -- p.8: the runtime knows all tool usage, and GPU when there is one -------

class ToolUser(Agent):
    async def run(self, ctx):
        await ctx.request_tool("filesystem", "write", path="x.txt", content="hi")
        await ctx.request_tool("filesystem", "read", path="x.txt")
        return "done"


class UsageAccountingTest(Base):
    async def test_tool_calls_are_recorded_for_the_whole_runtime(self):
        k = self.kernel(permissions={"*": ["filesystem"]},
                        tools={"filesystem": {"root": self.tmp.name}})
        await asyncio.wait_for(k.run_until_done(ToolUser()), timeout=30)
        usage = self.store.tool_usage()
        self.assertIn("filesystem", usage)
        self.assertEqual(usage["filesystem"]["calls"], 2)
        self.assertEqual(usage["filesystem"]["failed"], 0)

    async def test_model_usage_is_grouped_by_model(self):
        k = self.kernel(models={"classes": {"fast": [
            {"provider": "mock", "model": "mock-fast", "cost_per_mtok": [1, 1]}]}})

        await asyncio.wait_for(k.run_until_done(ModelCaller()), timeout=30)
        usage = self.store.model_usage()
        self.assertEqual(usage["mock-fast"]["calls"], 1)

    def test_gpu_reporting_never_raises_without_a_gpu(self):
        """The answer on a machine with no GPU is None, not an exception."""
        summary = gpu.summary()
        self.assertTrue(summary is None or "utilization" in summary)

    async def test_snapshot_carries_gpu_and_stays_serializable(self):
        from agentos.kernel.messages import assert_serializable
        snap = self.kernel().snapshot()
        self.assertIn("gpu", snap)
        assert_serializable("snapshot", snap)


if __name__ == "__main__":
    unittest.main(verbosity=2)
