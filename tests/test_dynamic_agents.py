"""Agents created at runtime, and the authority they are allowed to carry.

The point of this file is the ceiling. When a task's shape is decided by a
model rather than a programmer, "what could this possibly touch?" stops being
answerable by reading the code — so it has to be answerable from the kernel.
Attenuation is that answer: a parent delegates a subset of what it holds and
never more, so the root grant bounds the whole tree no matter how it grows.
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
from agentos.agents.llm import ActionError, LLMAgent  # noqa: E402
from agentos.agents.base import spec_of  # noqa: E402
from agentos.kernel.permissions import PermissionDenied  # noqa: E402
from agentos.kernel.states import AgentState  # noqa: E402
from agentos.kernel.store import Store  # noqa: E402


def scripted(*replies: object) -> dict:
    """A model class that answers with these replies, in order."""
    return [{"provider": "mock", "model": "scripted", "cost_per_mtok": [0, 0],
             "script": [r if isinstance(r, str) else json.dumps(r)
                        for r in replies]}]


class Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(self.tmp.name)
        self.addCleanup(self.store.close)
        self.root = Path(self.tmp.name) / "fs"
        self.root.mkdir()

    def kernel(self, models=None, **kw):
        kw.setdefault("tick", 0.01)
        kw.setdefault("permissions", {})
        kw.setdefault("tools", {"filesystem": {"root": str(self.root)}})
        return Kernel(store=self.store, models=models, **kw)


# -- delegation and its limits ----------------------------------------------

class Delegator(Agent):
    """Spawns one child with whatever `grant` it was told to try."""

    async def run(self, ctx):
        pid = await ctx.spawn(
            Probe(capability=self.params["probe"]), grant=self.params["grant"]
        )
        return await ctx.wait(pid)


class Probe(Agent):
    """Reports whether it can reach one capability."""

    async def run(self, ctx):
        try:
            await ctx.request_tool("filesystem", "list", path=".")
        except Exception as exc:
            return {"reached": False, "why": str(exc)}
        return {"reached": True}


class AttenuationTest(Base):
    async def test_a_parent_can_delegate_what_it_holds(self):
        k = self.kernel(permissions={"Delegator": ["filesystem"]})
        result = await asyncio.wait_for(
            k.run_until_done(Delegator(grant=["filesystem"], probe="filesystem")),
            timeout=30)
        self.assertTrue(result["reached"])

    async def test_a_parent_cannot_delegate_what_it_lacks(self):
        """The whole guarantee in one test: you cannot hand out authority you
        were never given."""
        k = self.kernel(permissions={"Delegator": ["http"]})
        pid = k.spawn(Delegator(grant=["filesystem"], probe="filesystem"))
        await asyncio.wait_for(k.run(), timeout=30)
        proc = k.table.get(pid)
        self.assertEqual(proc.state, AgentState.FAILED)
        self.assertIn("cannot grant filesystem", proc.exit_reason)

    async def test_a_child_granted_nothing_reaches_nothing(self):
        k = self.kernel(permissions={"Delegator": ["filesystem"]})
        result = await asyncio.wait_for(
            k.run_until_done(Delegator(grant=[], probe="filesystem")), timeout=30)
        self.assertFalse(result["reached"])
        self.assertIn("permission denied", result["why"].lower())

    async def test_a_pid_grant_overrides_a_wider_name_matrix(self):
        """A narrowed child must not be re-widened by its class name."""
        k = self.kernel(permissions={"Delegator": ["filesystem"],
                                     "Probe": ["filesystem"]})
        result = await asyncio.wait_for(
            k.run_until_done(Delegator(grant=[], probe="filesystem")), timeout=30)
        self.assertFalse(result["reached"])

    async def test_submit_spec_grant_sets_the_ceiling(self):
        k = self.kernel(permissions={})
        pid = k.submit_spec(spec_of(Probe(capability="filesystem")),
                            grant=["filesystem"])
        await asyncio.wait_for(k.run(), timeout=30)
        self.assertTrue(k.table.get(pid).result["reached"])

    async def test_grants_do_not_outlive_the_process(self):
        k = self.kernel(permissions={"Delegator": ["filesystem"]})
        await asyncio.wait_for(
            k.run_until_done(Delegator(grant=["filesystem"], probe="filesystem")),
            timeout=30)
        self.assertEqual(k.perms.pid_grants, {})


# -- the generic agent ------------------------------------------------------

class LLMAgentTest(Base):
    async def test_it_finishes_when_the_model_says_done(self):
        k = self.kernel(models={"classes": {"m": scripted({"action": "done",
                                                           "result": 42})}})
        result = await asyncio.wait_for(
            k.run_until_done(LLMAgent(role="R", goal="g", tools=[], model="m")),
            timeout=30)
        self.assertEqual(result, 42)

    async def test_it_uses_a_tool_and_reports_the_result(self):
        (self.root / "a.txt").write_text("hello", encoding="utf-8")
        k = self.kernel(models={"classes": {"m": scripted(
            {"action": "tool", "capability": "filesystem", "op": "read",
             "params": {"path": "a.txt"}},
            {"action": "done", "result": "read it"},
        )}})
        pid = k.submit_spec(
            spec_of(LLMAgent(role="Reader", goal="read", tools=["filesystem"],
                             model="m")),
            grant=["filesystem"])
        await asyncio.wait_for(k.run(), timeout=30)
        self.assertEqual(k.table.get(pid).result, "read it")

    async def test_flattened_tool_params_are_accepted(self):
        """Models flatten arguments as often as they nest them."""
        (self.root / "b.txt").write_text("hi", encoding="utf-8")
        k = self.kernel(models={"classes": {"m": scripted(
            {"action": "tool", "capability": "filesystem", "op": "read",
             "path": "b.txt"},
            {"action": "done", "result": "ok"},
        )}})
        pid = k.submit_spec(
            spec_of(LLMAgent(role="R", goal="g", tools=["filesystem"], model="m")),
            grant=["filesystem"])
        await asyncio.wait_for(k.run(), timeout=30)
        self.assertEqual(k.table.get(pid).result, "ok")

    async def test_a_tool_outside_its_set_is_refused_without_calling_it(self):
        k = self.kernel(models={"classes": {"m": scripted(
            {"action": "tool", "capability": "shell", "op": "run",
             "params": {"command": "echo hi"}},
            {"action": "done", "result": "gave up"},
        )}})
        result = await asyncio.wait_for(
            k.run_until_done(LLMAgent(role="R", goal="g", tools=["filesystem"],
                                      model="m")),
            timeout=30)
        self.assertEqual(result, "gave up")

    async def test_it_spawns_a_team_and_waits_for_it(self):
        k = self.kernel(models={"classes": {
            "planner": scripted(
                {"action": "spawn", "role": "Worker", "goal": "do it",
                 "tools": ["filesystem"]},
                {"action": "wait"},
                {"action": "done", "result": "team finished"},
            ),
            "worker": scripted({"action": "done", "result": "worker done"}),
        }})
        pid = k.submit_spec(
            spec_of(LLMAgent(role="Planner", goal="g", tools=["filesystem"],
                             model="planner", child_model="worker",
                             may_spawn=True)),
            grant=["filesystem"])
        await asyncio.wait_for(k.run(), timeout=60)
        self.assertEqual(k.table.get(pid).result, "team finished")
        names = {p.name for p in k.table.all()}
        self.assertIn("Worker", names)  # the role, not the class, in ps

    async def test_it_cannot_grant_a_child_more_than_it_holds(self):
        """Refused inside the agent, with a legible reason, before the kernel
        has to refuse it — the model gets a chance to retry smaller."""
        k = self.kernel(models={"classes": {
            "planner": scripted(
                {"action": "spawn", "role": "W", "goal": "g", "tools": ["shell"]},
                {"action": "done", "result": "backed off"},
            )}})
        result = await asyncio.wait_for(
            k.run_until_done(LLMAgent(role="P", goal="g", tools=["filesystem"],
                                      model="planner", may_spawn=True)),
            timeout=30)
        self.assertEqual(result, "backed off")
        self.assertEqual(len([p for p in k.table.all()]), 1)  # no child created

    async def test_a_worker_cannot_spawn(self):
        k = self.kernel(models={"classes": {"m": scripted(
            {"action": "spawn", "role": "X", "goal": "g", "tools": []},
            {"action": "done", "result": "could not"},
        )}})
        result = await asyncio.wait_for(
            k.run_until_done(LLMAgent(role="W", goal="g", tools=[], model="m")),
            timeout=30)
        self.assertEqual(result, "could not")

    async def test_it_gives_up_rather_than_looping_forever(self):
        k = self.kernel(models={"classes": {"m": scripted("not json at all")}})
        result = await asyncio.wait_for(
            k.run_until_done(LLMAgent(role="R", goal="g", tools=[], model="m",
                                      max_steps=3)),
            timeout=30)
        self.assertTrue(result["incomplete"])

    async def test_the_spec_of_a_dynamic_agent_round_trips(self):
        """A runtime-invented agent must still be re-creatable, or it cannot
        be journaled, recovered, or run in a subprocess."""
        from agentos.agents.base import agent_from_spec
        from agentos.kernel.messages import assert_serializable

        agent = LLMAgent(role="Surveyor", goal="measure", tools=["filesystem"],
                         model="m")
        spec = spec_of(agent)
        assert_serializable("spec", spec)
        again = agent_from_spec(spec)
        self.assertEqual(again.name, "Surveyor")
        self.assertEqual(again.params["tools"], ["filesystem"])


# -- events the parent wires, not the programmer -----------------------------

class Wirer(Agent):
    """Spawns one child with a declared event vocabulary."""

    async def run(self, ctx):
        pid = await ctx.spawn(
            Announcer(events=self.params["says"]),
            publishes=self.params["publishes"],
            subscribes=self.params.get("subscribes"),
        )
        return await ctx.wait(pid)


class Announcer(Agent):
    """Tries to publish whatever it was told to say."""

    async def run(self, ctx):
        said, refused = [], []
        for event in self.params["events"]:
            try:
                await ctx.publish(event, note="hi")
                said.append(event)
            except Exception as exc:
                refused.append(str(exc))
        return {"said": said, "refused": refused}


class EventWiringTest(Base):
    async def test_a_child_may_publish_what_its_parent_wired(self):
        result = await asyncio.wait_for(
            self.kernel().run_until_done(
                Wirer(publishes=["Ready"], says=["Ready"])),
            timeout=30)
        self.assertEqual(result["said"], ["Ready"])
        self.assertEqual(result["refused"], [])

    async def test_a_child_cannot_publish_a_name_it_was_not_wired_for(self):
        """The failure this whole mechanism exists to prevent: a model
        inventing a name nobody is listening for, and nobody noticing."""
        result = await asyncio.wait_for(
            self.kernel().run_until_done(
                Wirer(publishes=["Ready"], says=["Redy"])),
            timeout=30)
        self.assertEqual(result["said"], [])
        self.assertIn("was not wired to publish 'Redy'", result["refused"][0])

    async def test_a_child_wired_for_nothing_publishes_nothing(self):
        result = await asyncio.wait_for(
            self.kernel().run_until_done(Wirer(publishes=[], says=["Any"])),
            timeout=30)
        self.assertEqual(result["said"], [])

    async def test_a_hand_written_agent_is_unrestricted(self):
        """Declarations are for agents somebody else wired. An agent that was
        not wired decides its own events, as every example always has."""
        k = self.kernel()
        result = await asyncio.wait_for(
            k.run_until_done(Announcer(events=["Whatever", "IWant"])), timeout=30)
        self.assertEqual(result["said"], ["Whatever", "IWant"])

    async def test_the_wiring_shows_up_on_the_process(self):
        k = self.kernel()
        pid = k.spawn(Wirer(publishes=["Ready"], says=["Ready"]))
        await asyncio.wait_for(k.run(), timeout=30)
        child = [p for p in k.table.all() if p.parent == pid][0]
        self.assertEqual(child.row()["publishes"], ["Ready"])


class Slowpoke(Agent):
    async def run(self, ctx):
        await ctx.sleep(0.05)
        return "done"


class LateAnnouncer(Agent):
    """Publishes after a beat, so a waiter is listening when it does."""

    async def run(self, ctx):
        await ctx.sleep(0.05)
        await ctx.publish(self.params["event"], note="hi")
        return "announced"


class Hopeful(Agent):
    async def run(self, ctx):
        try:
            await ctx.wait_all(events=[self.params["event"]])
        except Exception as exc:
            return {"refused": str(exc)}
        return {"refused": None}


class UnsatisfiableWaitTest(Base):
    async def test_waiting_for_what_nobody_publishes_fails_instead_of_hanging(self):
        """With every live agent wired, an unpublishable name is provably a
        mistake — so it is refused now rather than found by the deadlock
        detector after everything has stalled."""
        k = self.kernel()
        pid = k.spawn(Wirer(publishes=["Ready"], says=["Ready"]))
        k.table.get(pid).publishes = ["Ready"]  # wire the root too
        hopeful = k.spawn(Hopeful(event="NeverComes"))
        k.table.get(hopeful).publishes = []
        await asyncio.wait_for(k.run(), timeout=30)
        self.assertIn("no live agent is wired to publish",
                      k.table.get(hopeful).result["refused"])

    async def test_a_kernel_event_is_always_waitable(self):
        """AgentFinished and friends have no declared publisher — the kernel
        emits them — so they must never be refused."""
        k = self.kernel()
        pid = k.spawn(Hopeful(event="AgentFinished"))
        k.table.get(pid).publishes = []
        # Sleeps first, so the waiter is certainly subscribed before this
        # finishes and the kernel fires AgentFinished. Without the sleep the
        # test races on which of the two reaches its syscall first.
        k.spawn(Slowpoke())
        await asyncio.wait_for(k.run(), timeout=30)
        self.assertIsNone(k.table.get(pid).result["refused"])

    async def test_an_unwired_agent_keeps_the_wait_open(self):
        """If anything alive could still publish it, the wait must stand."""
        k = self.kernel()
        pid = k.spawn(Hopeful(event="Maybe"))
        k.table.get(pid).publishes = []
        k.spawn(LateAnnouncer(event="Maybe"))  # unwired: may publish anything
        await asyncio.wait_for(k.run(), timeout=30)
        self.assertIsNone(k.table.get(pid).result["refused"])


class LLMEventTest(Base):
    async def test_a_planner_wires_two_agents_that_never_name_each_other(self):
        k = self.kernel(models={"classes": {
            "planner": scripted(
                {"action": "spawn", "role": "Producer", "goal": "make it",
                 "tools": [], "model": "producer", "publishes": ["DataReady"]},
                {"action": "spawn", "role": "Consumer", "goal": "use it",
                 "tools": [], "model": "consumer",
                 "publishes": ["Done"], "subscribes": ["DataReady"]},
                {"action": "wait", "events": ["Done"]},
                {"action": "done", "result": "pipeline finished"},
            ),
            "producer": scripted(
                {"action": "publish", "event": "DataReady", "payload": {"n": 1}},
                {"action": "done", "result": "produced"},
            ),
            "consumer": scripted(
                {"action": "publish", "event": "Done"},
                {"action": "done", "result": "consumed"},
            ),
        }})
        pid = k.submit_spec(
            spec_of(LLMAgent(role="Planner", goal="g", tools=[],
                             model="planner", may_spawn=True)))
        await asyncio.wait_for(k.run(), timeout=60)
        self.assertEqual(k.table.get(pid).result, "pipeline finished")
        flowed = {e["type"]: e["subscribers"] for e in self.store.events()}
        self.assertIn("DataReady", flowed)
        self.assertTrue(flowed["DataReady"], "DataReady woke nobody")

    async def test_a_worker_publishing_an_unwired_name_is_told_so(self):
        k = self.kernel(models={"classes": {
            "planner": scripted(
                {"action": "spawn", "role": "W", "goal": "g", "tools": [],
                 "model": "worker", "publishes": ["Right"]},
                {"action": "wait"},
                {"action": "done", "result": "finished anyway"},
            ),
            "worker": scripted(
                {"action": "publish", "event": "Wrong"},
                {"action": "done", "result": "corrected"},
            ),
        }})
        pid = k.submit_spec(
            spec_of(LLMAgent(role="P", goal="g", tools=[], model="planner",
                             may_spawn=True)))
        await asyncio.wait_for(k.run(), timeout=60)
        self.assertEqual(k.table.get(pid).result, "finished anyway")
        worker = [p for p in k.table.all() if p.name == "W"][0]
        self.assertEqual(worker.result, "corrected")


# -- the Context surface reached through actions ------------------------------

class StubMemory:
    def __init__(self):
        self.data: dict = {}

    async def store(self, key, value, kind="working"):
        self.data[(kind, key)] = value

    async def retrieve(self, key=None, kind="working", query=None, top=3, limit=20):
        if query is not None:
            return [{"key": "note", "value": "found by meaning"}]
        return self.data.get((kind, key))


class StubCtx:
    def __init__(self):
        self.memory = StubMemory()


class MemoryActionTest(unittest.IsolatedAsyncioTestCase):
    def agent(self, **params):
        params.setdefault("role", "R")
        params.setdefault("goal", "g")
        return LLMAgent(**params)

    async def test_remember_defaults_to_shared(self):
        ctx = StubCtx()
        out = await self.agent()._do_remember(
            ctx, {"key": "field", "value": 42})
        self.assertIn("whole team", out)
        self.assertEqual(ctx.memory.data[("shared", "field")], 42)

    async def test_private_maps_to_working(self):
        ctx = StubCtx()
        await self.agent()._do_remember(
            ctx, {"key": "note", "value": 1, "kind": "private"})
        self.assertIn(("working", "note"), ctx.memory.data)

    async def test_recall_checks_team_state_before_own_notes(self):
        ctx = StubCtx()
        ctx.memory.data[("shared", "k")] = "team value"
        ctx.memory.data[("working", "k")] = "my value"
        out = await self.agent()._do_recall(ctx, {"key": "k"})
        self.assertIn("team value", out)

    async def test_recall_by_query_searches_semantically(self):
        out = await self.agent()._do_recall(StubCtx(), {"query": "trees"})
        self.assertIn("found by meaning", out)

    async def test_recall_of_nothing_says_so(self):
        out = await self.agent()._do_recall(StubCtx(), {"key": "absent"})
        self.assertIn("Nothing is stored", out)

    async def test_bad_kind_is_refused_with_the_menu(self):
        out = await self.agent()._do_remember(
            StubCtx(), {"key": "k", "value": 1, "kind": "telepathic"})
        self.assertIn("shared", out)
        self.assertIn("Refused", out)


class MemoryIntegrationTest(Base):
    async def test_a_scripted_remember_lands_in_the_kernel_store(self):
        k = self.kernel(models={"classes": {"m": scripted(
            {"action": "remember", "key": "field-notes",
             "value": {"trees": 3}, "kind": "shared"},
            {"action": "done", "result": "noted"},
        )}})
        result = await asyncio.wait_for(
            k.run_until_done(LLMAgent(role="Surveyor", goal="g", model="m")),
            timeout=30)
        self.assertEqual(result, "noted")
        import json as _json
        row = self.store.db.execute(
            "SELECT value FROM memory WHERE mtype='shared' AND key='field-notes'"
        ).fetchone()
        self.assertEqual(_json.loads(row["value"]), {"trees": 3})


class ApprovalActionTest(Base):
    async def test_an_invented_agent_can_block_on_a_human(self):
        k = self.kernel(models={"classes": {"m": scripted(
            {"action": "ask_human", "role": "Operator", "reason": "deploying"},
            {"action": "done", "result": "released"},
        )}})
        pid = k.spawn(LLMAgent(role="Deployer", goal="ship", model="m"))
        run = asyncio.create_task(k.run())
        while k.table.get(pid).state is not AgentState.BLOCKED:
            await asyncio.sleep(0.01)
        self.assertIn("Operator", k.table.get(pid).waiting_on or "")
        k.approve(pid, "Operator")
        await asyncio.wait_for(run, timeout=30)
        self.assertEqual(k.table.get(pid).result, "released")


# -- retries that actually retry ---------------------------------------------

class UsesFlaky(Agent):
    """Module-level on purpose: a retried agent is re-created from its spec,
    and a class defined inside a test method has no importable qualname."""

    async def run(self, ctx):
        return await ctx.request_tool("flaky", "go")


class RetryReplayTest(Base):
    """A journaled failure must not replay as the same failure forever."""

    def setUp(self):
        super().setUp()
        from agentos.drivers import REGISTRY
        from agentos.drivers.base import ToolDriver, ToolError

        class FlakyDriver(ToolDriver):
            name = "flaky"

            def __init__(self, **kw):
                super().__init__(**kw)
                self.count = 0

            async def op_go(self):
                self.count += 1
                if self.count == 1:
                    raise ToolError("first call always fails")
                return "second call works"

        REGISTRY["flaky"] = FlakyDriver
        self.addCleanup(REGISTRY.pop, "flaky", None)

    async def test_a_retried_failure_reexecutes_instead_of_replaying(self):
        k = self.kernel(retries=1, permissions={"*": ["flaky"]})
        pid = k.spawn(UsesFlaky())
        await asyncio.wait_for(k.run(), timeout=30)
        proc = k.table.get(pid)
        self.assertEqual(proc.state, AgentState.FINISHED)
        self.assertEqual(proc.result, "second call works")
        self.assertEqual(proc.retries, 1)

    async def test_llmagent_exposes_its_retry_budget_to_the_kernel(self):
        self.assertEqual(LLMAgent(role="R", goal="g", retries=2).retries, 2)
        self.assertIsNone(LLMAgent(role="R", goal="g").retries)


class SpawnExtrasTest(Base):
    async def test_a_planner_can_prioritise_and_budget_a_child(self):
        k = self.kernel(models={"classes": {
            "planner": scripted(
                {"action": "spawn", "role": "Rush", "goal": "hurry",
                 "tools": [], "model": "worker",
                 "priority": "High", "retries": 99},
                {"action": "wait"},
                {"action": "done", "result": "ok"},
            ),
            "worker": scripted({"action": "done", "result": "done"}),
        }})
        pid = k.submit_spec(
            spec_of(LLMAgent(role="P", goal="g", tools=[], model="planner",
                             may_spawn=True)))
        await asyncio.wait_for(k.run(), timeout=60)
        child = [p for p in k.table.all() if p.name == "Rush"][0]
        self.assertEqual(child.priority, "High")
        self.assertEqual(child.spec["params"]["retries"], 3)  # clamped


# -- a wait whose publisher dies ----------------------------------------------

class SilentQuitter(Agent):
    """Wired as a publisher, exits without ever publishing."""

    async def run(self, ctx):
        await ctx.sleep(0.05)
        return "quit"


class OrphanedWaitTest(Base):
    async def test_a_wait_fails_when_its_last_publisher_exits(self):
        k = self.kernel()
        hopeful = k.spawn(Hopeful(event="NeverSaid"))
        k.table.get(hopeful).publishes = []
        quitter = k.spawn(SilentQuitter())
        k.table.get(quitter).publishes = ["NeverSaid"]
        await asyncio.wait_for(k.run(), timeout=30)
        refused = k.table.get(hopeful).result["refused"]
        self.assertIn("exited without publishing", refused)


class DecisionLogTest(Base):
    async def test_the_models_choices_appear_in_agent_logs(self):
        k = self.kernel(models={"classes": {"m": scripted(
            {"action": "remember", "key": "k", "value": 1},
            {"action": "done", "result": "ok"},
        )}})
        await asyncio.wait_for(
            k.run_until_done(LLMAgent(role="R", goal="g", model="m")),
            timeout=30)
        decisions = [l["message"] for l in self.store.logs()
                     if l["message"].startswith("decided:")]
        self.assertTrue(any("remember: k" in d for d in decisions))
        self.assertTrue(any("done" in d for d in decisions))


class ParsingTest(unittest.TestCase):
    def test_it_reads_json_out_of_a_fenced_block(self):
        action = LLMAgent._parse('```json\n{"action": "done", "result": 1}\n```')
        self.assertEqual(action["action"], "done")

    def test_it_reads_json_surrounded_by_prose(self):
        action = LLMAgent._parse('Sure! {"action": "wait"} — let me know.')
        self.assertEqual(action["action"], "wait")

    def test_prose_alone_is_an_error(self):
        with self.assertRaises(ActionError):
            LLMAgent._parse("I think we should probably measure the trees.")

    def test_json_without_an_action_key_is_an_error(self):
        with self.assertRaises(ActionError):
            LLMAgent._parse('{"result": "done"}')


if __name__ == "__main__":
    unittest.main(verbosity=2)
