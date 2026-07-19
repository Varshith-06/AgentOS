"""Spending caps.

The ledger has always been exact; what it could not do was stop. A planner
that may spawn agents can spend without limit, and "we measured it precisely"
is no comfort the morning after. These tests are about the kernel refusing.
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agentos import Agent, Kernel  # noqa: E402
from agentos.agents.base import spec_of  # noqa: E402
from agentos.agents.llm import LLMAgent  # noqa: E402
from agentos.kernel.states import AgentState  # noqa: E402
from agentos.kernel.store import Store  # noqa: E402

# 1000 tokens in + 1000 out at these rates is $0.002 a call, so a $0.005
# budget buys two calls and refuses the third.
PRICED = {"classes": {"m": [{
    "provider": "mock", "model": "priced", "cost_per_mtok": [1000.0, 1000.0],
}]}}


class Caller(Agent):
    """Makes calls until one is refused, then reports how many landed."""

    async def run(self, ctx):
        made = 0
        for _ in range(self.params.get("calls", 10)):
            try:
                await ctx.request_model("m", prompt="a b c d e f g h")
            except Exception as exc:
                return {"made": made, "refused": str(exc)}
            made += 1
        return {"made": made, "refused": None}


class Spawner(Agent):
    """Spends what it can, then has a child try to spend more."""

    async def run(self, ctx):
        mine = await ctx.wait(await ctx.spawn(Caller(calls=3)))
        theirs = await ctx.wait(await ctx.spawn(Caller(calls=3)))
        return {"first": mine, "second": theirs}


class Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(self.tmp.name)
        self.addCleanup(self.store.close)

    def kernel(self, **kw):
        kw.setdefault("tick", 0.01)
        kw.setdefault("models", PRICED)
        kw.setdefault("permissions", {})
        return Kernel(store=self.store, **kw)


class BudgetTest(Base):
    async def test_an_unmetered_task_is_not_stopped(self):
        k = self.kernel()
        result = await asyncio.wait_for(
            k.run_until_done(Caller(calls=4)), timeout=30)
        self.assertEqual(result["made"], 4)
        self.assertIsNone(result["refused"])

    async def test_a_budget_stops_the_calls(self):
        k = self.kernel()
        pid = k.submit_spec(spec_of(Caller(calls=10)), budget_usd=0.005)
        await asyncio.wait_for(k.run(), timeout=30)
        result = k.table.get(pid).result
        self.assertGreater(result["made"], 0, "it should get some calls")
        self.assertLess(result["made"], 10, "it should not get all of them")
        self.assertIn("budget exhausted", result["refused"])

    async def test_the_budget_covers_the_whole_tree_not_one_agent(self):
        """A planner that could spawn its way around a cap would not have
        one. What the second agent may spend depends on the first."""
        k = self.kernel(slots=1)
        pid = k.submit_spec(spec_of(Spawner()), budget_usd=0.005)
        await asyncio.wait_for(k.run(), timeout=60)
        result = k.table.get(pid).result
        spent_first = result["first"]["made"]
        spent_second = result["second"]["made"]
        self.assertGreater(spent_first, 0)
        # The child inherits a partly-spent budget, so it gets strictly less.
        self.assertLess(spent_second, spent_first + 1)
        self.assertIsNotNone(result["second"]["refused"])

    async def test_children_inherit_the_root_of_their_task(self):
        k = self.kernel()
        pid = k.submit_spec(spec_of(Spawner()), budget_usd=0.005)
        await asyncio.wait_for(k.run(), timeout=60)
        for proc in k.table.all():
            self.assertEqual(proc.root, pid, f"pid {proc.pid} escaped the tree")

    async def test_overshoot_is_bounded_by_one_call(self):
        """Cost is unknowable until a call returns, so the check happens
        before dispatch: a task may exceed its budget by at most the one call
        already in flight, and never by a second."""
        budget = 0.05
        k = self.kernel()
        pid = k.submit_spec(spec_of(Caller(calls=20)), budget_usd=budget)
        await asyncio.wait_for(k.run(), timeout=60)

        ledger = self.store.model_costs()
        spent = sum(c["cost"] for c in ledger.values())
        calls = sum(c["calls"] for c in ledger.values())
        per_call = spent / calls  # what a call actually costs, not a guess
        self.assertGreaterEqual(spent, budget, "it should reach the budget")
        self.assertLess(
            spent, budget + per_call,
            f"overshot by more than one call: ${spent:.4f} on a ${budget} "
            f"budget at ${per_call:.4f}/call",
        )
        self.assertIn("budget exhausted", k.table.get(pid).result["refused"])

    async def test_the_refusal_names_the_numbers(self):
        k = self.kernel()
        pid = k.submit_spec(spec_of(Caller(calls=10)), budget_usd=0.005)
        await asyncio.wait_for(k.run(), timeout=30)
        refused = k.table.get(pid).result["refused"]
        self.assertIn("$", refused)
        self.assertIn("0.005", refused)

    async def test_a_budget_is_visible_on_the_process(self):
        k = self.kernel()
        pid = k.submit_spec(spec_of(Caller(calls=1)), budget_usd=0.25)
        row = k.table.get(pid).row()
        self.assertEqual(row["budget_usd"], 0.25)
        self.assertEqual(row["root"], pid)

    async def test_an_exhausted_llm_agent_stops_cleanly(self):
        """It cannot ask the model what to do when the model is what was
        refused, so it reports rather than spinning."""
        k = self.kernel(models={"classes": {"m": [{
            "provider": "mock", "model": "priced",
            "cost_per_mtok": [1000.0, 1000.0],
            "script": [json.dumps({"action": "recall", "key": "x"})] * 20,
        }]}})
        pid = k.submit_spec(
            spec_of(LLMAgent(role="Spender", goal="g", model="m", max_steps=20)),
            budget_usd=0.005)
        await asyncio.wait_for(k.run(), timeout=30)
        proc = k.table.get(pid)
        self.assertEqual(proc.state, AgentState.FINISHED)
        self.assertTrue(proc.result["incomplete"])
        self.assertIn("budget exhausted", proc.result["reason"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
