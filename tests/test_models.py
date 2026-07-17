"""Phase 5b: model routing.

The bar (AgentOS.pdf p.16): model choice is a runtime configuration concern,
not an application concern. Agents ask for a capability class; the manager
selects by availability, falls through on failure, and the kernel records
tokens and cost per agent. All offline, via the mock provider.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agentos import Agent, Kernel, KernelError  # noqa: E402
from agentos.kernel.store import Store  # noqa: E402


class ModelUser(Agent):
    async def run(self, ctx):
        try:
            reply = await ctx.request_model(
                self.params.get("need", "fast"),
                prompt=self.params.get("prompt", "hello world"),
                system=self.params.get("system"),
            )
        except KernelError as exc:
            return {"error": str(exc)}
        return {"error": None, **reply}


class ModelTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def kernel(self, classes, **kw):
        return Kernel(store=self.store, tick=0.01, models={"classes": classes}, **kw)

    async def test_mock_round_trip_records_tokens_and_cost(self):
        k = self.kernel(
            {"fast": [{"provider": "mock", "model": "mock-1",
                       "cost_per_mtok": [1_000_000, 1_000_000]}]}  # $1 per token
        )
        pid = k.spawn(ModelUser(prompt="six words are in this prompt"))
        await asyncio.wait_for(k.run(), timeout=5)
        result = k.table.get(pid).result
        self.assertIsNone(result["error"])
        self.assertEqual(result["model"], "mock-1")
        self.assertTrue(result["text"])
        self.assertGreater(result["cost"], 0)

        costs = self.store.model_costs()
        self.assertIn(pid, costs)
        self.assertEqual(costs[pid]["calls"], 1)
        self.assertGreater(costs[pid]["cost"], 0)
        self.assertGreater(costs[pid]["input_tokens"], 0)

    async def test_routing_skips_unavailable_candidates(self):
        """A frontier model without its key is skipped, not fatal (p.7)."""
        k = self.kernel(
            {"fast": [
                {"provider": "anthropic", "model": "claude-haiku-4-5",
                 "api_key_env": "AGENTOS_TEST_KEY_THAT_IS_NOT_SET"},
                {"provider": "mock", "model": "mock-fallback"},
            ]}
        )
        result = await asyncio.wait_for(k.run_until_done(ModelUser()), timeout=5)
        self.assertEqual(result["model"], "mock-fallback")

    async def test_a_failing_candidate_falls_through_to_the_next(self):
        k = self.kernel(
            {"fast": [
                {"provider": "mock", "model": "mock-flaky", "simulate_failure": True},
                {"provider": "mock", "model": "mock-solid"},
            ]}
        )
        result = await asyncio.wait_for(k.run_until_done(ModelUser()), timeout=5)
        self.assertEqual(result["model"], "mock-solid")

    async def test_no_available_model_is_an_error_not_a_crash(self):
        k = self.kernel(
            {"fast": [{"provider": "anthropic", "model": "claude-haiku-4-5",
                       "api_key_env": "AGENTOS_TEST_KEY_THAT_IS_NOT_SET"}]}
        )
        pid = k.spawn(ModelUser())
        await asyncio.wait_for(k.run(), timeout=5)
        result = k.table.get(pid).result
        self.assertIn("no available model", result["error"])
        calls = self.store.model_calls()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["ok"], 0)  # the failure is on the record too

    async def test_an_unknown_need_lists_what_is_configured(self):
        k = self.kernel({"fast": [{"provider": "mock", "model": "m"}]})
        result = await asyncio.wait_for(
            k.run_until_done(ModelUser(need="telepathy")), timeout=5
        )
        self.assertIn("telepathy", result["error"])
        self.assertIn("fast", result["error"])  # tells you what exists

    async def test_context_window_routes_long_prompts_away(self):
        k = self.kernel(
            {"fast": [
                {"provider": "mock", "model": "mock-small", "context_window": 10},
                {"provider": "mock", "model": "mock-large", "context_window": 100000},
            ]}
        )
        result = await asyncio.wait_for(
            k.run_until_done(ModelUser(prompt="long " * 200)), timeout=5
        )
        self.assertEqual(result["model"], "mock-large")

    async def test_model_completion_is_an_event(self):
        k = self.kernel({"fast": [{"provider": "mock", "model": "m"}]})
        await asyncio.wait_for(k.run_until_done(ModelUser()), timeout=5)
        finished = [e for e in k.bus.history if e.type == "ModelFinished"]
        self.assertEqual(len(finished), 1)
        self.assertTrue(finished[0].payload["ok"])
        self.assertEqual(finished[0].payload["model"], "m")

    async def test_a_slow_model_call_is_a_wait_not_a_deadlock(self):
        """Every agent Waiting on a model in flight must not trip the stall
        detector — the pending call can still wake someone."""
        k = self.kernel(
            {"fast": [{"provider": "mock", "model": "m", "latency": 0.3}]}
        )
        pid = k.spawn(ModelUser())
        await asyncio.wait_for(k.run(), timeout=5)  # would fail fast if broken
        self.assertIsNone(k.table.get(pid).result["error"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
