"""Phase 5a: the memory manager.

The bar (AgentOS.pdf p.15): two agents share state through the kernel's memory
API and never touch each other. Plus the p.6 kinds: working memory is private
and dies with the process, longterm survives a restart, semantic retrieves by
similarity, episodic is the agent's own history.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agentos import Agent, Kernel  # noqa: E402
from agentos.kernel.store import Store  # noqa: E402


class Stranger(Agent):
    """Reads the same key as Recipient, but was never named in the share.

    A separate class rather than a subclass defined in the test: the two need
    different agent *names*, and a child process rebuilds an agent by
    importing it, so it must exist at module level.
    """

    async def run(self, ctx):
        await ctx.subscribe("KeycodeShared")
        await ctx.wait_event("KeycodeShared")
        return {"read": await ctx.memory.retrieve("keycode", kind="shared")}


class RoundTrip(Agent):
    """store -> retrieve one -> retrieve all -> delete -> confirm gone."""

    async def run(self, ctx):
        await ctx.memory.store("k", {"nested": [1, 2, 3]})
        everything = await ctx.memory.retrieve()  # no key: everything of mine
        one = await ctx.memory.retrieve("k")
        await ctx.memory.delete("k")
        gone = await ctx.memory.retrieve("k")
        return {"one": one, "all": everything, "gone": gone}


class Stasher(Agent):
    """Stores into its own working memory, then lingers."""

    async def run(self, ctx):
        await ctx.memory.store("secret", self.params.get("value", "classified"))
        await ctx.sleep(0.15)
        return "stashed"


class Snoop(Agent):
    """Tries to read a key another agent stored privately."""

    async def run(self, ctx):
        await ctx.sleep(0.05)  # the Stasher has definitely stored by now
        return {"stolen": await ctx.memory.retrieve("secret")}


class SharedWriter(Agent):
    async def run(self, ctx):
        await ctx.sleep(0.05)  # let readers subscribe
        await ctx.memory.store("finding", {"total": 42}, kind="shared")
        return "shared"


class SharedReader(Agent):
    async def run(self, ctx):
        await ctx.subscribe("MemoryUpdated")
        event = await ctx.wait_event("MemoryUpdated")
        return await ctx.memory.retrieve(event["key"], kind="shared")


class SelectiveSharer(Agent):
    """Shares a working key with exactly one pid, then announces."""

    async def run(self, ctx):
        await ctx.sleep(0.05)
        await ctx.memory.store("keycode", "1234")
        await ctx.memory.share("keycode", with_agent=self.params["friend"])
        await ctx.publish("KeycodeShared")
        return "done"


class Recipient(Agent):
    async def run(self, ctx):
        await ctx.subscribe("KeycodeShared")
        await ctx.wait_event("KeycodeShared")
        return {"read": await ctx.memory.retrieve("keycode", kind="shared")}


class Counter(Agent):
    """Increments a longterm counter; reports old and new."""

    async def run(self, ctx):
        runs = (await ctx.memory.retrieve("runs", kind="longterm")) or 0
        await ctx.memory.store("runs", runs + 1, kind="longterm")
        leftover = await ctx.memory.retrieve("leftover")  # working, prior run?
        await ctx.memory.store("leftover", "only for this run")
        return {"runs": runs + 1, "leftover_from_last_run": leftover}


class Librarian(Agent):
    async def run(self, ctx):
        await ctx.memory.store(
            "scheduler", "the scheduler picks which ready agent runs next",
            kind="semantic",
        )
        await ctx.memory.store(
            "fruit", "bananas are a yellow fruit rich in potassium",
            kind="semantic",
        )
        hits = await ctx.memory.retrieve(
            kind="semantic", query="which agent runs next on the scheduler", top=2
        )
        return [h["key"] for h in hits]


class Diarist(Agent):
    async def run(self, ctx):
        await ctx.log("a very memorable moment")
        history = await ctx.memory.retrieve(kind="episodic", limit=20)
        return any("memorable moment" in e["message"] for e in history)


class MemoryTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def kernel(self, **kw):
        return Kernel(store=self.store, tick=0.01, **kw)

    async def test_store_and_retrieve_round_trip(self):
        result = await asyncio.wait_for(
            self.kernel().run_until_done(RoundTrip()), timeout=5
        )
        self.assertEqual(result["one"], {"nested": [1, 2, 3]})
        self.assertEqual(result["all"], {"k": {"nested": [1, 2, 3]}})
        self.assertIsNone(result["gone"])

    async def test_working_memory_is_private(self):
        k = self.kernel()
        k.spawn(Stasher())
        snoop = k.spawn(Snoop())
        await asyncio.wait_for(k.run(), timeout=5)
        self.assertEqual(k.table.get(snoop).result, {"stolen": None})

    async def test_shared_memory_crosses_agents_through_the_kernel(self):
        """The p.15 bar: two agents share state and never touch each other."""
        k = self.kernel()
        reader = k.spawn(SharedReader())
        k.spawn(SharedWriter())
        await asyncio.wait_for(k.run(), timeout=5)
        self.assertEqual(k.table.get(reader).result, {"total": 42})
        self.assertIn("MemoryUpdated", [e.type for e in k.bus.history])

    async def test_share_grants_access_to_exactly_who_was_named(self):
        k = self.kernel()
        friend = k.spawn(Recipient())
        stranger = k.spawn(Stranger())
        k.spawn(SelectiveSharer(friend=friend))
        await asyncio.wait_for(k.run(), timeout=5)
        self.assertEqual(k.table.get(friend).result, {"read": "1234"})
        self.assertEqual(k.table.get(stranger).result, {"read": None})

    async def test_longterm_memory_survives_a_restart(self):
        first = await asyncio.wait_for(
            self.kernel().run_until_done(Counter()), timeout=5
        )
        self.assertEqual(first["runs"], 1)

        second = await asyncio.wait_for(
            self.kernel().run_until_done(Counter()), timeout=5  # a fresh runtime
        )
        self.assertEqual(second["runs"], 2, "longterm memory must survive")
        self.assertIsNone(
            second["leftover_from_last_run"], "working memory must not"
        )

    async def test_semantic_retrieval_ranks_by_similarity(self):
        result = await asyncio.wait_for(
            self.kernel().run_until_done(Librarian()), timeout=5
        )
        self.assertEqual(result[0], "scheduler")

    async def test_episodic_memory_is_the_agents_own_history(self):
        result = await asyncio.wait_for(
            self.kernel().run_until_done(Diarist()), timeout=5
        )
        self.assertTrue(result)

    async def test_private_memory_is_freed_at_exit(self):
        k = self.kernel()
        pid = k.spawn(Stasher(value="ephemeral"))
        await asyncio.wait_for(k.run(), timeout=5)
        rows = self.store.db.execute(
            "SELECT * FROM memory WHERE mtype = 'working' AND owner = ?", (str(pid),)
        ).fetchall()
        self.assertEqual(rows, [], "a dead process holds no private memory")


if __name__ == "__main__":
    unittest.main(verbosity=2)
