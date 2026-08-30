"""Phase 5a: the memory manager."""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agentos import Agent, Kernel  # noqa: E402
from agentos.kernel import store as memory_store  # noqa: E402
from agentos.kernel.store import Store, unpack_vector  # noqa: E402

from tests._timing import LIMIT, STARTUP  # noqa: E402


class Stranger(Agent):
    """Reads the same key as Recipient, but was never named in the share."""

    async def run(self, ctx):
        await ctx.subscribe("KeycodeShared")
        await ctx.wait_event("KeycodeShared")
        return {"read": await ctx.memory.retrieve("keycode", kind="shared")}


class RoundTrip(Agent):
    """store -> retrieve one -> retrieve all -> delete -> confirm gone."""

    async def run(self, ctx):
        await ctx.memory.store("k", {"nested": [1, 2, 3]})
        everything = await ctx.memory.retrieve()
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
        await ctx.sleep(0.05)
        return {"stolen": await ctx.memory.retrieve("secret")}


class SharedWriter(Agent):
    async def run(self, ctx):
        await ctx.sleep(STARTUP)
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
        await ctx.sleep(STARTUP)
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
        leftover = await ctx.memory.retrieve("leftover")
        await ctx.memory.store("leftover", "only for this run")
        return {"runs": runs + 1, "leftover_from_last_run": leftover}


class Librarian(Agent):
    """Files text into longterm memory and asks for it back by meaning."""

    async def run(self, ctx):
        await ctx.memory.store(
            "scheduler", "the scheduler picks which ready agent runs next",
            kind="longterm",
        )
        await ctx.memory.store(
            "fruit", "bananas are a yellow fruit rich in potassium",
            kind="longterm",
        )
        await ctx.memory.store("tally", {"count": 3}, kind="longterm")
        hits = await ctx.memory.retrieve(
            kind="longterm", query="which agent runs next on the scheduler", top=3
        )
        return {
            "ranked": [h["key"] for h in hits],
            "by_key": await ctx.memory.retrieve("tally", kind="longterm"),
        }


class Cataloguer(Agent):
    """Files a spread of texts, then reports the ranking in full."""

    NOTES = {
        "scheduler": "the scheduler picks which ready agent runs next",
        "memory": "working memory dies with the process that wrote it",
        "kernel": "the kernel is the only writer of runtime state",
        "fruit": "bananas are a yellow fruit rich in potassium",
        "weather": "it rained for most of the afternoon",
    }

    async def run(self, ctx):
        for key, text in self.NOTES.items():
            await ctx.memory.store(key, text, kind="longterm")
        return await ctx.memory.retrieve(
            kind="longterm", query=self.params["query"], top=3
        )


class Forgetful(Agent):
    """Files three notes, then withdraws one and overwrites another."""

    async def run(self, ctx):
        for key in ("kept", "dropped", "retyped"):
            await ctx.memory.store(key, f"a note about {key}", kind="longterm")
        await ctx.memory.delete("dropped", kind="longterm")
        await ctx.memory.store("retyped", {"no": "text"}, kind="longterm")
        return True


class Antiquarian(Agent):
    """Asks for the two kinds that used to exist, and reports the refusals."""

    async def run(self, ctx):
        errors = {}
        for kind in ("scratchpad", "semantic"):
            try:
                await ctx.memory.store("k", "v", kind=kind)
            except Exception as exc:
                errors[kind] = str(exc)
        return errors


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
            self.kernel().run_until_done(RoundTrip()), timeout=LIMIT
        )
        self.assertEqual(result["one"], {"nested": [1, 2, 3]})
        self.assertEqual(result["all"], {"k": {"nested": [1, 2, 3]}})
        self.assertIsNone(result["gone"])

    async def test_working_memory_is_private(self):
        k = self.kernel()
        k.spawn(Stasher())
        snoop = k.spawn(Snoop())
        await asyncio.wait_for(k.run(), timeout=LIMIT)
        self.assertEqual(k.table.get(snoop).result, {"stolen": None})

    async def test_shared_memory_crosses_agents_through_the_kernel(self):
        """The p.15 bar: two agents share state and never touch each other."""
        k = self.kernel()
        reader = k.spawn(SharedReader())
        k.spawn(SharedWriter())
        await asyncio.wait_for(k.run(), timeout=LIMIT)
        self.assertEqual(k.table.get(reader).result, {"total": 42})
        self.assertIn("MemoryUpdated", [e.type for e in k.bus.history])

    async def test_share_grants_access_to_exactly_who_was_named(self):
        k = self.kernel()
        friend = k.spawn(Recipient())
        stranger = k.spawn(Stranger())
        k.spawn(SelectiveSharer(friend=friend))
        await asyncio.wait_for(k.run(), timeout=LIMIT)
        self.assertEqual(k.table.get(friend).result, {"read": "1234"})
        self.assertEqual(k.table.get(stranger).result, {"read": None})

    async def test_longterm_memory_survives_a_restart(self):
        first = await asyncio.wait_for(
            self.kernel().run_until_done(Counter()), timeout=LIMIT
        )
        self.assertEqual(first["runs"], 1)

        second = await asyncio.wait_for(
            self.kernel().run_until_done(Counter()), timeout=LIMIT
        )
        self.assertEqual(second["runs"], 2, "longterm memory must survive")
        self.assertIsNone(
            second["leftover_from_last_run"], "working memory must not"
        )

    async def test_longterm_text_is_retrievable_by_meaning(self):
        result = await asyncio.wait_for(
            self.kernel().run_until_done(Librarian()), timeout=LIMIT
        )
        self.assertEqual(result["ranked"][0], "scheduler")
        self.assertNotIn(
            "tally", result["ranked"], "a value with no text has nothing to match"
        )
        self.assertEqual(
            result["by_key"], {"count": 3}, "longterm still stores any JSON by key"
        )

    async def test_retired_kinds_name_their_replacement(self):
        k = self.kernel()
        pid = k.spawn(Antiquarian())
        await asyncio.wait_for(k.run(), timeout=LIMIT)
        errors = k.table.get(pid).result
        self.assertIn("working", errors["scratchpad"])
        self.assertIn("longterm", errors["semantic"])

    async def test_episodic_memory_is_the_agents_own_history(self):
        result = await asyncio.wait_for(
            self.kernel().run_until_done(Diarist()), timeout=LIMIT
        )
        self.assertTrue(result)

    async def test_private_memory_is_freed_at_exit(self):
        k = self.kernel()
        pid = k.spawn(Stasher(value="ephemeral"))
        await asyncio.wait_for(k.run(), timeout=LIMIT)
        rows = self.store.db.execute(
            "SELECT * FROM memory WHERE mtype = 'working' AND owner = ?", (str(pid),)
        ).fetchall()
        self.assertEqual(rows, [], "a dead process holds no private memory")

    async def test_longterm_embeddings_are_stored_as_packed_floats(self):
        await asyncio.wait_for(
            self.kernel().run_until_done(Librarian()), timeout=LIMIT
        )
        rows = self.store.db.execute(
            "SELECT typeof(vector) AS t, vector FROM memory"
            " WHERE mtype = 'longterm' AND vector IS NOT NULL"
        ).fetchall()
        self.assertTrue(rows, "Librarian stores text, so it stores embeddings")
        for r in rows:
            self.assertEqual(r["t"], "blob")
            self.assertEqual(len(unpack_vector(r["vector"])), 128)

    async def test_json_embeddings_from_an_older_db_are_migrated(self):
        self.store.db.execute(
            "INSERT INTO memory VALUES ('longterm', 'ann', 'k', ?, ?, NULL, 'ann', 0)",
            (json.dumps("hello"), json.dumps([0.5, 0.25, 0.125] + [0.0] * 125)),
        )
        self.store.register_runtime("fair", 4)
        row = self.store.db.execute(
            "SELECT typeof(vector) AS t, vector FROM memory WHERE key = 'k'"
        ).fetchone()
        self.assertEqual(row["t"], "blob", "startup rewrites text vectors")
        self.assertEqual(unpack_vector(row["vector"])[:3], (0.5, 0.25, 0.125))

    def test_episodic_memory_is_bounded(self):
        store = self.store
        with mock.patch.object(memory_store, "LOG_LIMIT", 200),                 mock.patch.object(memory_store, "_TRIM_EVERY", 50):
            for i in range(2000):
                store.append_log(7, "info", f"m{i}")
        rows = store.db.execute("SELECT COUNT(*) AS c FROM log").fetchone()
        self.assertLessEqual(rows["c"], 250, "the log is a ring buffer")
        self.assertEqual(store.logs(pid=7, limit=1)[0]["message"], "m1999")


class VectorIndexTest(unittest.IsolatedAsyncioTestCase):
    """sqlite-vec is optional, so the scan it replaces must stay equivalent."""

    def setUp(self):
        if memory_store.sqlite_vec is None:
            self.skipTest("sqlite-vec is not installed")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)  # LIFO: after every store.close()

    def store(self, indexed: bool) -> Store:
        with mock.patch.object(
            memory_store, "sqlite_vec", memory_store.sqlite_vec if indexed else None
        ):
            store = Store(Path(self.tmp.name) / ("on" if indexed else "off"))
        self.assertIs(store.vec, indexed)
        self.addCleanup(store.close)
        return store

    async def ranked(self, store: Store, query: str) -> list[dict]:
        return await asyncio.wait_for(
            Kernel(store=store, tick=0.01).run_until_done(Cataloguer(query=query)),
            timeout=LIMIT,
        )

    async def test_the_index_ranks_exactly_as_the_scan_does(self):
        for query in ("scheduler", "a stack of unrelated words"):
            indexed = await self.ranked(self.store(True), query)
            scanned = await self.ranked(self.store(False), query)
            self.assertEqual(
                [r["score"] for r in indexed],
                [r["score"] for r in scanned],
                f"{query!r} must rank the same with and without sqlite-vec",
            )

    async def test_the_index_tracks_the_table_it_mirrors(self):
        store = self.store(True)
        await asyncio.wait_for(
            Kernel(store=store, tick=0.01).run_until_done(Forgetful()), timeout=LIMIT
        )
        indexed = {
            r["mkey"] for r in store.db.execute("SELECT mkey FROM memory_vec")
        }
        table = {
            r["key"]
            for r in store.db.execute(
                "SELECT key FROM memory"
                " WHERE mtype = 'longterm' AND vector IS NOT NULL"
            )
        }
        self.assertEqual(indexed, table, "a delete must unindex too")
        self.assertEqual(indexed, {"kept"}, "and a re-store without text must too")

    def test_a_stale_index_is_rebuilt_at_startup(self):
        store = self.store(True)
        store.db.execute(
            "INSERT INTO memory VALUES ('longterm', 'ann', 'k', ?, ?, NULL, 'ann', 0)",
            (json.dumps("scheduler notes"), memory_store.pack_vector([0.1] * 128)),
        )
        store.register_runtime("fair", 4)  # the index knew nothing of that row
        self.assertEqual(
            [k for k, _ in store.nearest("ann", memory_store.pack_vector([0.1] * 128), 5)],
            ["k"],
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
