"""Phase 2: event bus, dependency graph, scheduling policies, deadlock."""

from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from collections import deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agentos import Agent, Kernel, KernelError  # noqa: E402
from agentos.agents.base import DirectInvocationError  # noqa: E402
from agentos.kernel.process import ProcessTable  # noqa: E402
from agentos.kernel.scheduler import (  # noqa: E402
    DependencyAware,
    Priority,
    SchedulerView,
)
from agentos.kernel.states import AgentState  # noqa: E402
from agentos.kernel.store import Store  # noqa: E402


class Publisher(Agent):
    async def run(self, ctx):
        await ctx.sleep(self.params.get("delay", 0.02))
        await ctx.publish("ResearchCompleted", topic="vectors", findings="3 sources")
        return "published"


class Subscriber(Agent):
    async def run(self, ctx):
        await ctx.subscribe("ResearchCompleted")
        event = await ctx.wait_event("ResearchCompleted")
        return f"{self.params['tag']} woke on {event['topic']}"


class BusySubscriber(Agent):
    """Subscribes, then goes away long enough to miss the publish."""

    async def run(self, ctx):
        await ctx.subscribe("ResearchCompleted")
        await ctx.sleep(0.15)  # the event fires while we are asleep
        event = await ctx.wait_event("ResearchCompleted")
        return f"buffered: {event['topic']}"


class Orchestrator(Agent):
    async def run(self, ctx):
        subs = [await ctx.spawn(Subscriber(tag=t)) for t in self.params["tags"]]
        await ctx.spawn(Publisher())
        result = await ctx.wait_all(agents=subs)
        return result["agents"]


class Waiter(Agent):
    async def run(self, ctx):
        return await ctx.wait_all(
            agents=self.params.get("agents", []),
            events=self.params.get("events", []),
            timer=self.params.get("timer"),
        )


class Hopeful(Agent):
    async def run(self, ctx):
        await ctx.wait_event("NeverPublished")
        return "unreachable"


class Stubborn(Agent):
    async def run(self, ctx):
        try:
            await ctx.wait(self.params["target"])
        except KernelError as exc:
            return {"refused": str(exc)}
        return {"refused": None}


class Circular(Agent):
    async def run(self, ctx):
        other = await ctx.spawn(Stubborn(target=ctx.pid))
        return await ctx.wait(other)


class Rude(Agent):
    """Tries to call another agent directly instead of spawning it."""

    async def run(self, ctx):
        victim = Subscriber(tag="victim")
        try:
            await victim.run(ctx)
        except DirectInvocationError as exc:
            return {"blocked": str(exc)}
        return {"blocked": None}


class Marker(Agent):
    async def run(self, ctx):
        await ctx.log(f"ran {self.params['tag']}")
        return self.params["tag"]


class EventTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def kernel(self, **kw):
        return Kernel(store=self.store, tick=0.01, **kw)

    # -- the event bus (p.5) ---------------------------------------------
    async def test_publisher_wakes_subscribers_it_never_names(self):
        k = self.kernel()
        results = await asyncio.wait_for(
            k.run_until_done(Orchestrator(tags=["code", "docs"])), timeout=5
        )
        self.assertEqual(
            sorted(results.values()),
            ["code woke on vectors", "docs woke on vectors"],
        )

    async def test_adding_a_subscriber_changes_nothing_else(self):
        """The loose-coupling claim: a third subscriber, same publisher code."""
        k = self.kernel()
        results = await asyncio.wait_for(
            k.run_until_done(Orchestrator(tags=["code", "docs", "review"])), timeout=5
        )
        self.assertEqual(len(results), 3)

    async def test_event_is_buffered_for_a_busy_subscriber(self):
        """No subscribe/publish race: an event fired while asleep still lands."""
        k = self.kernel()
        pid = k.spawn(BusySubscriber())
        k.spawn(Publisher(delay=0.02))  # fires long before the subscriber waits
        await asyncio.wait_for(k.run(), timeout=5)
        self.assertIs(k.table.get(pid).state, AgentState.FINISHED)
        self.assertEqual(k.table.get(pid).result, "buffered: vectors")

    async def test_kernel_publishes_agent_lifecycle_events(self):
        k = self.kernel()
        await asyncio.wait_for(k.run_until_done(Publisher()), timeout=5)
        types = [e.type for e in k.bus.history]
        self.assertIn("TimerExpired", types)
        self.assertIn("ResearchCompleted", types)
        self.assertIn("AgentFinished", types)

    # -- the dependency graph (p.5) --------------------------------------
    async def test_wait_all_resolves_agents_events_and_timer_together(self):
        k = self.kernel()
        pub = k.spawn(Publisher(delay=0.02))
        waiter = k.spawn(
            Waiter(agents=[pub], events=["ResearchCompleted"], timer=0.05)
        )
        await asyncio.wait_for(k.run(), timeout=5)
        result = k.table.get(waiter).result
        self.assertEqual(result["agents"], {pub: "published"})
        self.assertEqual(result["events"]["ResearchCompleted"]["topic"], "vectors")
        self.assertTrue(result["timer"])

    async def test_waiter_wakes_only_after_every_dependency(self):
        k = self.kernel()
        slow = k.spawn(Publisher(delay=0.1))
        waiter = k.spawn(Waiter(agents=[slow], events=["ResearchCompleted"]))
        await asyncio.wait_for(k.run(), timeout=5)
        # It cannot have finished before the thing it depended on.
        self.assertGreaterEqual(
            k.table.get(waiter).ended_at, k.table.get(slow).ended_at
        )

    async def test_wait_all_with_no_dependencies_is_rejected(self):
        k = self.kernel()
        pid = k.spawn(Waiter())
        await asyncio.wait_for(k.run(), timeout=5)
        self.assertIs(k.table.get(pid).state, AgentState.FAILED)
        self.assertIn("at least one dependency", k.table.get(pid).exit_reason)

    # -- deadlock --------------------------------------------------------
    async def test_wait_cycle_is_refused_not_hung(self):
        k = self.kernel()
        await asyncio.wait_for(k.run_until_done(Circular()), timeout=5)
        stubborn = k.table.get(2)
        self.assertIs(stubborn.state, AgentState.FINISHED)
        self.assertIn("deadlock", stubborn.result["refused"])
        self.assertTrue(
            any(e["kind"] == "deadlock" for e in self.store.logs()),
            "the refusal must be visible in the kernel log",
        )

    async def test_unsatisfiable_event_wait_is_detected_not_hung(self):
        k = self.kernel()
        pid = k.spawn(Hopeful())
        await asyncio.wait_for(k.run(), timeout=5)  # would hang forever if broken
        proc = k.table.get(pid)
        self.assertIs(proc.state, AgentState.FAILED)
        self.assertIn("deadlock", proc.exit_reason)

    async def test_a_pending_timer_is_not_a_deadlock(self):
        """An agent asleep can still publish. Do not cry deadlock on it."""
        k = self.kernel()
        waiter = k.spawn(Waiter(events=["ResearchCompleted"]))
        k.spawn(Publisher(delay=0.2))  # everyone is Waiting/Sleeping meanwhile
        await asyncio.wait_for(k.run(), timeout=5)
        self.assertIs(k.table.get(waiter).state, AgentState.FINISHED)

    # -- p.5: agents never directly invoke other agents -------------------
    async def test_direct_agent_invocation_is_blocked(self):
        k = self.kernel()
        result = await asyncio.wait_for(k.run_until_done(Rude()), timeout=5)
        self.assertIn("Agents are processes, not functions", result["blocked"])

    # -- scheduling policies (p.4) ---------------------------------------
    def _ready(self, *procs):
        return deque(procs)

    def test_priority_policy_prefers_high(self):
        table = ProcessTable()
        low = table.create("L", {}, priority="Low")
        high = table.create("H", {}, priority="High")
        normal = table.create("N", {}, priority="Normal")
        picked = Priority().pick(self._ready(low, high, normal), SchedulerView())
        self.assertIs(picked, high)

    def test_priority_policy_ages_to_prevent_starvation(self):
        table = ProcessTable()
        policy = Priority()
        low = table.create("L", {}, priority="Low")
        ready = self._ready(low)
        for _ in range(Priority.ANTI_STARVATION_PICKS):
            high = table.create("H", {}, priority="High")
            ready.append(high)
            self.assertIs(policy.pick(ready, SchedulerView()), high)
        ready.append(table.create("H", {}, priority="High"))
        self.assertIs(policy.pick(ready, SchedulerView()), low)  # its turn at last

    def test_dependency_aware_runs_whoever_unblocks_the_most(self):
        table = ProcessTable()
        lonely = table.create("Lonely", {})
        blocking = table.create("Blocking", {})
        view = SchedulerView(dependents={blocking.pid: 3})
        picked = DependencyAware().pick(self._ready(lonely, blocking), view)
        self.assertIs(picked, blocking)  # despite being submitted second

    async def test_priority_policy_end_to_end(self):
        k = self.kernel(policy="priority", slots=1)
        for tag, prio in [("low", "Low"), ("normal", "Normal"), ("high", "High")]:
            agent = Marker(tag=tag)
            agent.priority = prio
            k.spawn(agent)
        await asyncio.wait_for(k.run(), timeout=5)
        order = [
            e["message"].removeprefix("ran ")
            for e in self.store.logs()
            if e["kind"] == "agent"
        ]
        self.assertEqual(order, ["high", "normal", "low"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
