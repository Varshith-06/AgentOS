"""A call outlives its caller: the reply is taken, not the call made again.

Two crashes, two answers, neither asking anything of the provider:

  the agent dies, the kernel lives: the kernel sees the call through and gives
  the restarted agent its reply;

  the runtime dies: the call was carried by a courier, a process of its own,
  and the recovered runtime takes the courier's reply.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agentos import Agent, Kernel  # noqa: E402
from agentos.drivers import REGISTRY, ToolDriver  # noqa: E402
from agentos.kernel.memory import MemoryManager  # noqa: E402
from agentos.kernel.store import Store  # noqa: E402
from agentos.runtime.courier import Couriers  # noqa: E402

from tests._paid import bill  # noqa: E402
from tests._timing import LIMIT  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


class Billed(ToolDriver):
    """Bills on receipt and honours no key. A courier can make the call too."""

    name = "billed"
    timeout = 60.0
    courier = {"charge": "tests._paid:bill"}

    async def op_charge(self, n: int, db: str, gate: str | None = None) -> dict:
        return await asyncio.to_thread(bill, n, db, gate)


class Biller(Agent):
    """Charges once. Its first life writes its OS pid, to be killed by it."""

    retries = 1

    async def run(self, ctx):
        Path(self.params["pidfile"]).write_text(str(os.getpid()))
        flag = Path(self.params["pidfile"] + ".lived")
        second = flag.exists()
        flag.write_text("1")
        if second and self.params.get("dawdle"):
            await asyncio.sleep(self.params["dawdle"])  # not a syscall: no journal
        if second and self.params.get("change"):
            await ctx.log("decided otherwise")
            out = await ctx.request_tool(
                "billed", "charge", n=8, db=self.params["db"], gate=self.params["gate"])
            return out["charged"]
        out = await ctx.request_tool(
            "billed", "charge", n=7, db=self.params["db"], gate=self.params["gate"])
        return out["charged"]


class Asker(Agent):
    async def run(self, ctx):
        out = await ctx.request_model("fast", "say something")
        return out["text"]


class Model(BaseHTTPRequestHandler):
    """A provider: bills on receipt, answers once `gate` is set, keys ignored."""

    received: list[dict]
    gate: threading.Event

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.received.append(body)
        n = len(self.received)
        self.gate.wait(LIMIT)
        reply = json.dumps({
            "id": f"gen-{n}", "choices": [{"message": {"content": f"reply {n}"}}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2},
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(reply)))
        self.end_headers()
        self.wfile.write(reply)


class AdoptionTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.store = Store(self.dir / "store")
        self.bills = str(self.dir / "bills.db")
        conn = sqlite3.connect(self.bills)
        conn.execute("CREATE TABLE charges (n INTEGER)")
        conn.commit()
        conn.close()
        self.gate = str(self.dir / "gate")
        self.pidfile = str(self.dir / "agent.pid")
        REGISTRY["billed"] = Billed
        self.kernels: list[Kernel] = []
        self.couriers: list[Couriers] = []

    def tearDown(self):
        REGISTRY.pop("billed", None)
        Path(self.gate).touch()  # release any courier still waiting
        for k in self.kernels:
            if k.couriers is not None:
                k.couriers.close()
        for c in self.couriers:
            c.close()
        self.store.close()
        self.tmp.cleanup()

    def kernel(self, **kw) -> Kernel:
        k = Kernel(store=self.store, tick=0.01, tools={"billed": {}},
                   permissions={"*": ["billed"]}, **kw)
        self.kernels.append(k)
        return k

    def biller(self, **kw) -> Biller:
        return Biller(db=self.bills, gate=self.gate, pidfile=self.pidfile, **kw)

    def charges(self) -> list[int]:
        conn = sqlite3.connect(self.bills, timeout=30)
        try:
            return [r[0] for r in conn.execute("SELECT n FROM charges")]
        finally:
            conn.close()

    def said(self, pid: int) -> list[str]:
        return [r["message"] for r in self.store.logs(pid=pid)]

    def flagged(self, k: Kernel) -> list:
        return [e for e in k.bus.history if e.type == "OutcomeAmbiguous"]

    async def until(self, predicate, timeout=LIMIT):
        async def poll():
            while not predicate():
                await asyncio.sleep(0.01)

        await asyncio.wait_for(poll(), timeout)

    async def crash(self, k: Kernel, run_task: asyncio.Task) -> None:
        """kill -9, in-process: every task of the runtime dies, nothing cleans up.
        A courier is a process of its own and is not among them."""
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

    # -- the agent dies, the kernel lives -------------------------------------

    async def agent_dies_mid_call(self, k: Kernel, agent: Biller):
        pid = k.spawn(agent)
        run = asyncio.create_task(k.run())
        await self.until(lambda: len(self.charges()) == 1)
        os.kill(int(Path(self.pidfile).read_text()), signal.SIGTERM)
        return pid, run

    async def test_a_reply_that_lands_before_the_restart_asks_is_replayed(self):
        k = self.kernel()
        pid, run = await self.agent_dies_mid_call(k, self.biller(dawdle=1.5))
        await self.until(lambda: any("still in flight" in m for m in self.said(pid)))
        Path(self.gate).touch()  # the call ends while the new life dawdles
        await asyncio.wait_for(run, timeout=LIMIT)
        self.assertEqual(k.table.get(pid).result, 7)
        self.assertEqual(self.charges(), [7], "made once")
        self.assertTrue(any("journaled for the restarted agent" in m for m in self.said(pid)),
                        self.said(pid))
        self.assertEqual(self.flagged(k), [])
        self.assertEqual(self.store.open_intents(), {})

    async def test_a_restart_that_asks_for_something_else_drops_the_old_reply(self):
        k = self.kernel()
        pid, run = await self.agent_dies_mid_call(k, self.biller(change=True))
        await self.until(lambda: any("decided otherwise" in m for m in self.said(pid)))
        Path(self.gate).touch()
        await asyncio.wait_for(run, timeout=LIMIT)
        self.assertEqual(k.table.get(pid).result, 8, "the new call's reply, not the old")
        self.assertEqual(sorted(self.charges()), [7, 8])
        self.assertTrue(any("never asked for again" in m for m in self.said(pid)),
                        self.said(pid))
        self.assertEqual(self.store.open_intents(), {})

    # -- the runtime dies; a courier carried the call -------------------------

    async def runtime_dies_mid_call(self, **kw):
        k1 = self.kernel(courier=True, **kw)
        pid = k1.spawn(self.biller())
        run1 = asyncio.create_task(k1.run())
        await self.until(lambda: len(self.charges()) == 1)
        await self.crash(k1, run1)
        return pid

    def courier_row(self) -> dict:
        c = Couriers(self.store.dir)
        try:
            return dict(c.db.execute("SELECT * FROM calls ORDER BY rowid DESC").fetchone())
        finally:
            c.close()

    async def test_a_courier_makes_the_call_and_the_kernel_takes_its_reply(self):
        Path(self.gate).touch()
        k = self.kernel(courier=True)
        pid = k.spawn(self.biller())
        await asyncio.wait_for(k.run(), timeout=LIMIT)
        self.assertEqual(k.table.get(pid).result, 7)
        self.assertEqual(self.charges(), [7])
        row = self.courier_row()
        self.assertEqual(row["state"], "done")
        self.assertNotEqual(row["pid"], os.getpid(), "made in a process of its own")
        self.assertEqual(self.store.open_intents(), {})

    async def test_a_runtime_crash_mid_call_takes_the_couriers_reply(self):
        pid = await self.runtime_dies_mid_call()
        self.assertEqual(self.courier_row()["state"], "running", "outlived the runtime")
        k2 = self.kernel(courier=True, recover=True)
        run2 = asyncio.create_task(k2.run())
        await self.until(lambda: any("is still at it" in m for m in self.said(pid)))
        Path(self.gate).touch()
        await asyncio.wait_for(run2, timeout=LIMIT)
        self.assertEqual(k2.table.get(pid).result, 7)
        self.assertEqual(self.charges(), [7], "no key, and still made once")
        self.assertEqual(self.flagged(k2), [])
        self.assertEqual(self.store.open_intents(), {})

    async def test_a_courier_that_finished_before_recovery_is_adopted(self):
        pid = await self.runtime_dies_mid_call()
        Path(self.gate).touch()
        await self.until(lambda: self.courier_row()["state"] == "done")
        k2 = self.kernel(courier=True, recover=True)
        await asyncio.wait_for(k2.run(), timeout=LIMIT)
        self.assertEqual(k2.table.get(pid).result, 7)
        self.assertEqual(self.charges(), [7])
        self.assertTrue(any("had finished" in m for m in self.said(pid)), self.said(pid))

    async def test_a_courier_that_died_mid_call_is_flagged_and_the_call_made_again(self):
        pid = await self.runtime_dies_mid_call()
        os.kill(self.courier_row()["pid"], signal.SIGTERM)  # the machine, say
        Path(self.gate).touch()
        k2 = self.kernel(courier=True, recover=True)
        await asyncio.wait_for(k2.run(), timeout=LIMIT)
        self.assertEqual(k2.table.get(pid).result, 7)
        self.assertEqual(self.charges(), [7, 7], "nobody could account for it")
        self.assertEqual(len(self.flagged(k2)), 1, "and the repeat is on the record")

    async def test_a_courier_that_never_claimed_its_call_never_makes_it(self):
        c = Couriers(self.store.dir)
        self.couriers.append(c)
        c.db.execute(
            "INSERT INTO calls (key, intent, target, state, created)"
            " VALUES ('9.1', 9, 'tests._paid:bill', 'dispatched', 0)")
        self.assertEqual(c.fate(9)[0], "unsent")
        # The courier turns up late, after recovery wrote it off.
        request = json.dumps({"db": str(c.path), "dir": str(c.dir), "key": "9.1",
                              "target": "tests._paid:bill",
                              "args": {"n": 9, "db": self.bills}})
        subprocess.run([sys.executable, "-m", "agentos.runtime.courier"], input=request.encode(),
                       cwd=ROOT, check=True, timeout=LIMIT)
        self.assertEqual(self.charges(), [], "it never makes a call it lost the claim to")
        self.assertEqual(c.row("9.1")["state"], "abandoned")

    async def test_a_model_call_by_courier_survives_a_runtime_crash(self):
        Model.received, Model.gate = [], threading.Event()
        server = ThreadingHTTPServer(("127.0.0.1", 0), Model)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.addCleanup(Model.gate.set)
        models = {"classes": {"fast": [{
            "provider": "openai", "model": "m", "api_key_env": None,
            "base_url": f"http://127.0.0.1:{server.server_address[1]}/v1",
            "cost_per_mtok": [1.0, 1.0]}]}}

        k1 = self.kernel(courier=True, models=models)
        pid = k1.spawn(Asker())
        run1 = asyncio.create_task(k1.run())
        await self.until(lambda: len(Model.received) == 1)
        await self.crash(k1, run1)

        k2 = self.kernel(courier=True, models=models, recover=True)
        run2 = asyncio.create_task(k2.run())
        await self.until(lambda: any("is still at it" in m for m in self.said(pid)))
        Model.gate.set()
        await asyncio.wait_for(run2, timeout=LIMIT)
        self.assertEqual(k2.table.get(pid).result, "reply 1", "the first call's reply")
        self.assertEqual(len(Model.received), 1, "the provider saw one request")
        self.assertEqual(self.flagged(k2), [])
        cost = self.store.model_costs()[pid]["cost"]
        self.assertAlmostEqual(cost, 5 / 1e6, msg="priced from the adopted reply")


if __name__ == "__main__":
    unittest.main()
