"""The hosted path: a sentence and a tool list arrive over HTTP.

This is the endpoint that turns AgentOS into something a company can run
behind an API, so most of these tests are about what it *refuses*. The goal
text and the tool list come off a socket; the planner they produce can invent
agents nobody wrote. The ceiling has to hold at the door.
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agentos import RuntimeClient  # noqa: E402
from agentos.kernel.store import Store  # noqa: E402
from agentos.runtime.daemon import Daemon  # noqa: E402


def scripted(*replies: object) -> list:
    return [{"provider": "mock", "model": "scripted", "cost_per_mtok": [0, 0],
             "script": [r if isinstance(r, str) else json.dumps(r)
                        for r in replies]}]


MODELS = {"classes": {
    "fast": scripted(
        {"action": "spawn", "role": "Surveyor", "goal": "measure",
         "tools": ["filesystem"]},
        {"action": "wait"},
        {"action": "done", "result": "experiment complete"},
    ),
    "worker": scripted(
        {"action": "tool", "capability": "filesystem", "op": "write",
         "params": {"path": "notes.txt", "content": "oak 12m"}},
        {"action": "done", "result": "measured"},
    ),
}}


class TaskApiTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(self.tmp.name)
        self.root = Path(self.tmp.name) / "fs"
        self.root.mkdir()
        self.daemon = Daemon(
            store=self.store, port=0, tick=0.01, isolation="task",
            models=MODELS, permissions={},
            tools={"filesystem": {"root": str(self.root)}},
            task_tools=["filesystem"],   # what the operator allows
        )
        self.task = asyncio.create_task(self.daemon.start())
        await asyncio.sleep(0.15)
        self.client = RuntimeClient(url=self.daemon.url)

    async def asyncTearDown(self):
        self.daemon.stop()
        await asyncio.wait_for(self.task, timeout=20)
        self.store.close()

    def post(self, path: str, body: dict) -> tuple[int, dict]:
        req = urllib.request.Request(
            self.daemon.url + path, method="POST",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    # -- the happy path ------------------------------------------------------
    async def test_a_sentence_and_a_tool_list_produce_a_team(self):
        pid = await asyncio.to_thread(
            self.client.task, "perform an experiment about trees",
            ["filesystem"], "fast", child_model="worker")
        result = await asyncio.to_thread(self.client.wait, pid, 60)
        self.assertEqual(result, "experiment complete")

        tree = await asyncio.to_thread(self.client.task_tree, pid)
        self.assertEqual(tree["status"], "Finished")
        self.assertEqual(tree["goal"], "perform an experiment about trees")
        # The planner invented an agent nobody declared, and it did real work.
        self.assertIn("Surveyor", [a["name"] for a in tree["agents"]])
        self.assertEqual((self.root / "notes.txt").read_text(encoding="utf-8"),
                         "oak 12m")

    async def test_the_grant_is_the_ceiling_for_invented_agents(self):
        pid = await asyncio.to_thread(
            self.client.task, "measure", ["filesystem"], "fast",
            child_model="worker")
        await asyncio.to_thread(self.client.wait, pid, 60)
        tree = await asyncio.to_thread(self.client.task_tree, pid)
        for agent in tree["agents"] + [await asyncio.to_thread(
                self.client.status, pid)]:
            self.assertTrue(set(agent["permissions"]) <= {"filesystem"})

    # -- what it refuses -----------------------------------------------------
    async def test_a_tool_the_operator_did_not_allow_is_refused(self):
        code, body = await asyncio.to_thread(
            self.post, "/task", {"goal": "run something", "tools": ["shell"]})
        self.assertEqual(code, 400)
        self.assertIn("does not allow shell", body["error"])

    async def test_an_unknown_capability_is_refused(self):
        code, body = await asyncio.to_thread(
            self.post, "/task", {"goal": "x", "tools": ["telepathy"]})
        self.assertEqual(code, 400)
        self.assertIn("unknown capability", body["error"])

    async def test_an_empty_goal_is_refused(self):
        for goal in ("", "   ", None, 42):
            code, body = await asyncio.to_thread(
                self.post, "/task", {"goal": goal, "tools": []})
            self.assertEqual(code, 400, f"goal={goal!r} should be refused")

    async def test_an_enormous_goal_is_refused(self):
        code, body = await asyncio.to_thread(
            self.post, "/task", {"goal": "x" * 5000, "tools": []})
        self.assertEqual(code, 400)
        self.assertIn("longer than", body["error"])

    async def test_a_malformed_tool_list_is_refused(self):
        code, _ = await asyncio.to_thread(
            self.post, "/task", {"goal": "x", "tools": "filesystem"})
        self.assertEqual(code, 400)

    async def test_step_limits_are_clamped_not_trusted(self):
        """A caller cannot ask for a million model calls."""
        code, body = await asyncio.to_thread(
            self.post, "/task",
            {"goal": "x", "tools": [], "max_steps": 10 ** 9})
        self.assertEqual(code, 200)
        row = await asyncio.to_thread(self.client.status, body["pid"])
        self.assertLessEqual(row["spec"]["params"]["max_steps"], 50)

    async def test_a_negative_step_count_is_refused(self):
        code, _ = await asyncio.to_thread(
            self.post, "/task", {"goal": "x", "tools": [], "max_steps": -1})
        self.assertEqual(code, 400)

    async def test_priority_and_retries_are_accepted_and_clamped(self):
        code, body = await asyncio.to_thread(
            self.post, "/task",
            {"goal": "x", "tools": [], "priority": "High", "retries": 99})
        self.assertEqual(code, 200)
        row = await asyncio.to_thread(self.client.status, body["pid"])
        self.assertEqual(row["priority"], "High")
        self.assertEqual(row["spec"]["params"]["retries"], 5)  # clamped

    async def test_a_budget_over_the_operators_ceiling_is_refused(self):
        """The daemon in this fixture allows no budget ceiling, so a request
        naming one is accepted; the ceiling case is covered below."""
        code, body = await asyncio.to_thread(
            self.post, "/task", {"goal": "x", "tools": [], "budget_usd": 0.25})
        self.assertEqual(code, 200)
        self.assertEqual(body["budget_usd"], 0.25)

    async def test_a_nonsense_budget_is_refused(self):
        for bad in (0, -1, "lots", True):
            code, _ = await asyncio.to_thread(
                self.post, "/task",
                {"goal": "x", "tools": [], "budget_usd": bad})
            self.assertEqual(code, 400, f"budget {bad!r} was accepted")

    async def test_explicit_null_is_unmetered_when_no_ceiling_is_set(self):
        code, body = await asyncio.to_thread(
            self.post, "/task", {"goal": "x", "tools": [], "budget_usd": None})
        self.assertEqual(code, 200)
        self.assertIsNone(body["budget_usd"])

    async def test_a_nonsense_priority_is_refused(self):
        code, body = await asyncio.to_thread(
            self.post, "/task", {"goal": "x", "tools": [], "priority": "Urgent"})
        self.assertEqual(code, 400)
        self.assertIn("priority", body["error"])

    # -- POST /agents now carries a grant too --------------------------------
    async def test_submit_accepts_a_grant(self):
        from agentos.agents.llm import LLMAgent

        pid = await asyncio.to_thread(
            self.client.submit,
            LLMAgent(role="Solo", goal="just finish", tools=[], model="worker"),
            None, ["filesystem"])
        row = await asyncio.to_thread(self.client.status, pid)
        self.assertEqual(row["permissions"], ["filesystem"])

    async def test_a_malformed_grant_is_refused(self):
        from agentos.agents.base import spec_of
        from agentos.agents.llm import LLMAgent

        code, _ = await asyncio.to_thread(
            self.post, "/agents",
            {"spec": spec_of(LLMAgent(role="X", goal="y")), "grant": "filesystem"})
        self.assertEqual(code, 400)


class BudgetCeilingTest(unittest.IsolatedAsyncioTestCase):
    """A daemon started with --task-budget caps what a caller may ask for."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(self.tmp.name)
        self.daemon = Daemon(store=self.store, port=0, tick=0.01,
                             isolation="task", models=MODELS, permissions={},
                             task_budget_usd=0.10)
        self.task = asyncio.create_task(self.daemon.start())
        await asyncio.sleep(0.15)

    async def asyncTearDown(self):
        self.daemon.stop()
        await asyncio.wait_for(self.task, timeout=20)
        self.store.close()

    def post(self, body):
        req = urllib.request.Request(
            self.daemon.url + "/task", method="POST",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    async def test_asking_for_more_than_the_ceiling_is_refused(self):
        code, body = await asyncio.to_thread(
            self.post, {"goal": "x", "tools": [], "budget_usd": 5.0})
        self.assertEqual(code, 400)
        self.assertIn("caps submitted tasks", body["error"])

    async def test_asking_for_less_is_allowed(self):
        code, body = await asyncio.to_thread(
            self.post, {"goal": "x", "tools": [], "budget_usd": 0.02})
        self.assertEqual(code, 200)
        self.assertEqual(body["budget_usd"], 0.02)

    async def test_the_ceiling_applies_when_none_is_requested(self):
        code, body = await asyncio.to_thread(
            self.post, {"goal": "x", "tools": []})
        self.assertEqual(code, 200)
        self.assertEqual(body["budget_usd"], 0.10)

    async def test_asking_for_unmetered_cannot_bypass_the_ceiling(self):
        """A cap you can opt out of by asking is not a cap."""
        code, body = await asyncio.to_thread(
            self.post, {"goal": "x", "tools": [], "budget_usd": None})
        self.assertEqual(code, 400)
        self.assertIn("not allowed", body["error"])


class NoToolsAllowedTest(unittest.IsolatedAsyncioTestCase):
    """A daemon started without --task-tools grants submitted tasks nothing."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(self.tmp.name)
        self.daemon = Daemon(store=self.store, port=0, tick=0.01,
                             isolation="task", models=MODELS, permissions={})
        self.task = asyncio.create_task(self.daemon.start())
        await asyncio.sleep(0.15)

    async def asyncTearDown(self):
        self.daemon.stop()
        await asyncio.wait_for(self.task, timeout=20)
        self.store.close()

    async def test_the_default_is_no_tools_at_all(self):
        req = urllib.request.Request(
            self.daemon.url + "/task", method="POST",
            data=json.dumps({"goal": "x", "tools": ["filesystem"]}).encode(),
            headers={"Content-Type": "application/json"})

        def send():
            try:
                with urllib.request.urlopen(req, timeout=10) as r:
                    return r.status, json.loads(r.read())
            except urllib.error.HTTPError as exc:
                return exc.code, json.loads(exc.read())

        code, body = await asyncio.to_thread(send)
        self.assertEqual(code, 400)
        self.assertIn("no tools at all", body["error"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
