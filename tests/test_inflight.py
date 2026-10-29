"""A call in flight at a crash: re-issued once under its key, or flagged, never silent."""

from __future__ import annotations

import asyncio
import os
import signal
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agentos import Agent, Kernel  # noqa: E402
from agentos.drivers import REGISTRY, ToolDriver  # noqa: E402
from agentos.drivers.sql import SQL  # noqa: E402
from agentos.kernel.memory import MemoryManager  # noqa: E402
from agentos.kernel.states import AgentState  # noqa: E402
from agentos.kernel.store import Store  # noqa: E402

from tests._timing import LIMIT  # noqa: E402


class Paid(ToolDriver):
    """A paid API: bills on receipt, answers after `latency`, honours keys."""

    name = "paid"
    idempotent = ("charge",)

    def __init__(self, db: str, latency: float = 0.0, **kw):
        super().__init__(**kw)
        self.db = db
        self.latency = latency

    async def op_charge(self, n: int, idempotency_key: str | None = None) -> dict:
        conn = sqlite3.connect(self.db, isolation_level=None)
        try:
            if idempotency_key is not None:
                seen = conn.execute(
                    "SELECT n FROM keys WHERE key = ?", (idempotency_key,)
                ).fetchone()
                if seen is not None:
                    return {"charged": seen[0], "replayed": True}
                conn.execute("INSERT INTO keys VALUES (?, ?)", (idempotency_key, n))
            conn.execute("INSERT INTO charges VALUES (?)", (n,))
        finally:
            conn.close()
        await asyncio.sleep(self.latency)
        return {"charged": n}


class Unkeyed(Paid):
    """The same API without idempotency support: a repeat is a second charge."""

    name = "unkeyed"
    idempotent = ()


class Charger(Agent):
    async def run(self, ctx):
        try:
            out = await ctx.request_tool(self.params["api"], "charge", n=7)
        except Exception as exc:
            return f"refused: {exc}"
        return out["charged"]


class Doomed(Agent):
    """Charges once; its own process is killed while the charge is in flight."""

    retries = 1

    async def run(self, ctx):
        Path(self.params["pidfile"]).write_text(str(os.getpid()))
        out = await ctx.request_tool(self.params["api"], "charge", n=7)
        return out["charged"]


class Fickle(Agent):
    """Charges on its first life; after a recovery it changes its mind."""

    async def run(self, ctx):
        if Path(self.params["flag"]).exists():
            await ctx.log("decided against charging")
            return "changed its mind"
        Path(self.params["flag"]).write_text("1")
        await ctx.request_tool("paid", "charge", n=7)
        return "charged"


class InFlightTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.bills = str(Path(self.tmp.name) / "bills.db")
        conn = sqlite3.connect(self.bills)
        conn.execute("CREATE TABLE charges (n INTEGER)")
        conn.execute("CREATE TABLE keys (key TEXT PRIMARY KEY, n INTEGER)")
        conn.commit()
        conn.close()
        REGISTRY["paid"], REGISTRY["unkeyed"] = Paid, Unkeyed

    def tearDown(self):
        REGISTRY.pop("paid", None)
        REGISTRY.pop("unkeyed", None)
        self.store.close()
        self.tmp.cleanup()

    def kernel(self, api, latency, **kw):
        return Kernel(
            store=self.store, tick=0.01,
            tools={api: {"db": self.bills, "latency": latency}},
            permissions={"*": [api]}, **kw,
        )

    def charges(self) -> int:
        conn = sqlite3.connect(self.bills)
        try:
            return conn.execute("SELECT COUNT(*) FROM charges").fetchone()[0]
        finally:
            conn.close()

    async def _until(self, predicate, timeout=LIMIT):
        async def poll():
            while not predicate():
                await asyncio.sleep(0.01)

        await asyncio.wait_for(poll(), timeout)

    async def _crash(self, k, run_task):
        """kill -9, in-process: every task dies instantly, nothing cleans up."""
        scratch_dir = tempfile.TemporaryDirectory()
        self.addCleanup(scratch_dir.cleanup)
        scratch = Store(scratch_dir.name)
        self.addCleanup(scratch.close)
        k.store = scratch
        k.memory = MemoryManager(scratch)
        tasks = [run_task]
        tasks += [p.task for p in k.table.all() if p.task is not None]
        tasks += list(k._io_tasks)
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _crash_mid_call(self, api, **kw):
        """Bill, then die before the reply: the outcome nobody recorded."""
        k1 = self.kernel(api, latency=30.0)
        pid = k1.spawn(Charger(api=api))
        run1 = asyncio.create_task(k1.run())
        await self._until(lambda: self.charges() == 1)
        self.assertIs(k1.table.get(pid).state, AgentState.WAITING)
        await self._crash(k1, run1)

        k2 = self.kernel(api, latency=0.0, recover=True, **kw)
        await asyncio.wait_for(k2.run(), timeout=LIMIT)
        return k2, pid

    async def test_an_intent_is_open_while_the_call_runs_and_settled_after(self):
        k = self.kernel("paid", latency=0.3)
        pid = k.spawn(Charger(api="paid"))
        run = asyncio.create_task(k.run())
        await self._until(lambda: self.charges() == 1)
        self.assertIn(pid, self.store.open_intents())
        await asyncio.wait_for(run, timeout=LIMIT)
        self.assertEqual(self.store.open_intents(), {})

    async def test_an_idempotent_call_in_flight_takes_effect_once(self):
        k2, pid = await self._crash_mid_call("paid")
        self.assertEqual(k2.table.get(pid).result, 7)
        self.assertEqual(
            self.charges(), 1,
            "re-issued under the same key, the API must not charge again",
        )
        self.assertEqual(self.store.open_intents(), {})
        self.assertEqual(
            [e for e in k2.bus.history if e.type == "OutcomeAmbiguous"], [],
            "a keyed re-issue is not ambiguous",
        )

    async def test_an_unkeyed_call_in_flight_is_re_issued_and_flagged(self):
        k2, pid = await self._crash_mid_call("unkeyed")
        self.assertEqual(k2.table.get(pid).result, 7)
        self.assertEqual(self.charges(), 2, "at-least-once: it ran twice")
        flagged = [e for e in k2.bus.history if e.type == "OutcomeAmbiguous"]
        self.assertEqual(len(flagged), 1, "the repeat must be on the record")
        self.assertEqual(flagged[0].payload["target"], "unkeyed.charge")

    async def test_the_fail_policy_refuses_to_guess(self):
        k2, pid = await self._crash_mid_call("unkeyed", on_ambiguous="fail")
        self.assertTrue(str(k2.table.get(pid).result).startswith("refused:"))
        self.assertIn("may or may not have taken effect", k2.table.get(pid).result)
        self.assertEqual(self.charges(), 1, "nothing was re-issued")

    async def test_completed_calls_still_replay_from_the_journal(self):
        k1 = self.kernel("unkeyed", latency=0.0)
        await asyncio.wait_for(k1.run_until_done(Charger(api="unkeyed")), LIMIT)
        k2 = self.kernel("unkeyed", latency=0.0, recover=True)
        await asyncio.wait_for(k2.run(), timeout=LIMIT)
        self.assertEqual(self.charges(), 1)

    async def _agent_dies_mid_call(self, api, **kw):
        pidfile = Path(self.tmp.name) / "agent.pid"
        k = self.kernel(api, latency=1.0, **kw)
        pid = k.spawn(Doomed(api=api, pidfile=str(pidfile)))
        run = asyncio.create_task(k.run())
        await self._until(lambda: self.charges() == 1)
        os.kill(int(pidfile.read_text()), signal.SIGTERM)  # the agent, not the kernel
        await asyncio.wait_for(run, timeout=LIMIT)
        return k, pid

    async def test_a_restarted_agent_re_issues_its_call_under_the_same_key(self):
        k, pid = await self._agent_dies_mid_call("paid", adopt=False)
        self.assertEqual(k.table.get(pid).result, 7)
        self.assertEqual(k.table.get(pid).retries, 1, "it really was restarted")
        self.assertEqual(self.charges(), 1)

    async def test_without_adoption_a_restarted_agent_flags_an_unkeyed_repeat(self):
        k, pid = await self._agent_dies_mid_call("unkeyed", adopt=False)
        self.assertEqual(k.table.get(pid).result, 7)
        self.assertEqual(self.charges(), 2)
        self.assertEqual(
            len([e for e in k.bus.history if e.type == "OutcomeAmbiguous"]), 1)

    async def test_a_restarted_agent_is_given_its_call_in_flight_not_a_second_one(self):
        k, pid = await self._agent_dies_mid_call("unkeyed")
        self.assertEqual(k.table.get(pid).result, 7)
        self.assertEqual(k.table.get(pid).retries, 1, "it really was restarted")
        self.assertEqual(self.charges(), 1, "no key, and still made once")
        self.assertEqual(
            [e for e in k.bus.history if e.type == "OutcomeAmbiguous"], [],
            "nothing is ambiguous: the kernel saw the call through",
        )
        self.assertEqual(self.store.open_intents(), {})
        said = [r["message"] for r in self.store.logs(pid=pid)]
        self.assertTrue(any("is still in flight" in m for m in said), said)

    async def test_a_call_never_asked_for_again_is_settled_and_said(self):
        flag = str(Path(self.tmp.name) / "flag")
        k1 = self.kernel("paid", latency=30.0)
        pid = k1.spawn(Fickle(flag=flag))
        run1 = asyncio.create_task(k1.run())
        await self._until(lambda: self.charges() == 1)
        await self._crash(k1, run1)

        k2 = self.kernel("paid", latency=0.0, recover=True)
        await asyncio.wait_for(k2.run(), timeout=LIMIT)
        self.assertEqual(k2.table.get(pid).result, "changed its mind")
        self.assertEqual(self.store.open_intents(), {})
        said = [r["message"] for r in self.store.logs(pid=pid)]
        self.assertTrue(any("never asked for again" in m for m in said), said)

    def test_on_ambiguous_must_be_a_known_policy(self):
        with self.assertRaises(ValueError):
            Kernel(store=self.store, on_ambiguous="hope")


class SqlEffectsTest(unittest.IsolatedAsyncioTestCase):
    """The sql driver applies a keyed statement and records it in one commit."""

    async def test_a_repeated_key_is_applied_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "t.db")
            driver = SQL(db=db)
            await driver.execute(
                "execute", {"statement": "CREATE TABLE t (x INTEGER)"}
            )
            params = {"statement": "INSERT INTO t VALUES (1)"}
            first = await driver.execute("execute", params, idempotency_key="k1")
            again = await driver.execute("execute", params, idempotency_key="k1")
            other = await driver.execute("execute", params, idempotency_key="k2")
            rows = await driver.execute("query", {"query": "SELECT COUNT(*) AS n FROM t"})
            driver._conn.close()
        self.assertEqual(first, {"rowcount": 1})
        self.assertEqual(again, {"rowcount": 1, "deduplicated": True})
        self.assertEqual(other, {"rowcount": 1})
        self.assertEqual(rows, [{"n": 2}])

    async def test_a_failed_statement_leaves_no_key_behind(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "t.db")
            driver = SQL(db=db)
            with self.assertRaises(Exception):
                await driver.execute(
                    "execute", {"statement": "INSERT INTO missing VALUES (1)"},
                    idempotency_key="k",
                )
            await driver.execute("execute", {"statement": "CREATE TABLE missing (x)"})
            out = await driver.execute(
                "execute", {"statement": "INSERT INTO missing VALUES (1)"},
                idempotency_key="k",
            )
            driver._conn.close()
        self.assertEqual(out, {"rowcount": 1}, "a rolled-back key must not dedup")


if __name__ == "__main__":
    unittest.main(verbosity=2)
