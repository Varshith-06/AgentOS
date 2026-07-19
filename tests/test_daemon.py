"""Phase 7: the shared runtime daemon, thin clients, and OS-process agents.

The bar (AgentOS.pdf p.17): global visibility across independent applications
— two clients submit to one runtime and one ps shows both, costs aggregated.
Plus the executor swap: agents in real OS subprocesses, with agents/ and
kernel/ unchanged.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agentos import Agent, Kernel, RemoteAgentFailed, RuntimeClient  # noqa: E402
from agentos.kernel.store import Store  # noqa: E402
from agentos.runtime.daemon import Daemon  # noqa: E402


class Napper(Agent):
    async def run(self, ctx):
        await ctx.log(f"working for {self.params['app']}")
        await ctx.sleep(self.params.get("nap", 0.05))
        return {"app": self.params["app"]}


class Biller(Agent):
    async def run(self, ctx):
        reply = await ctx.request_model("fast", prompt="hello from " + self.params["app"])
        return {"app": self.params["app"], "cost": reply["cost"]}


class WhoAmI(Agent):
    async def run(self, ctx):
        await ctx.log("checking my address space")
        return {"os_pid": os.getpid(), "agent_pid": ctx.pid}


class Family(Agent):
    """Spawns a subprocess child from inside a subprocess: full syscalls."""

    async def run(self, ctx):
        await ctx.memory.store("note", "written from a real process")
        child = await ctx.spawn(WhoAmI())
        child_result = await ctx.wait(child)
        note = await ctx.memory.retrieve("note")
        return {
            "my_os_pid": os.getpid(),
            "child_os_pid": child_result["os_pid"],
            "note": note,
        }


class DaemonTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # Cleanups run LIFO: the daemon (registered later, in _daemon) must
        # stop BEFORE the store it is ticking against gets closed.
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(self.tmp.name)
        self.addCleanup(self.store.close)

    async def _daemon(self, **kw):
        
        kw.setdefault("models", {"classes": {"fast": [
            {"provider": "mock", "model": "mock-1",
             "cost_per_mtok": [1_000_000, 1_000_000]},  # $1/token: visible cost
        ]}})
        d = Daemon(store=self.store, host="127.0.0.1", port=0, tick=0.01, **kw)
        task = asyncio.create_task(d.start())

        async def stop():
            d.stop()
            await asyncio.wait_for(task, timeout=10)

        self.addAsyncCleanup(stop)
        await asyncio.sleep(0.05)  # let the kernel loop spin up
        return d

    # -- the p.17 bar ---------------------------------------------------------
    async def test_two_applications_one_runtime_one_table(self):
        d = await self._daemon()
        app1 = RuntimeClient(url=d.url)
        app2 = RuntimeClient(url=d.url)

        pid1 = await asyncio.to_thread(app1.submit, Napper(app="research"))
        pid2 = await asyncio.to_thread(app2.submit, Napper(app="support"))
        r1 = await asyncio.to_thread(app1.wait, pid1, 10)
        r2 = await asyncio.to_thread(app2.wait, pid2, 10)
        self.assertEqual((r1["app"], r2["app"]), ("research", "support"))

        ps = await asyncio.to_thread(app1.ps)  # either client sees everyone
        pids = {row["pid"] for row in ps["processes"]}
        self.assertEqual(pids, {pid1, pid2})

    async def test_costs_aggregate_across_applications(self):
        d = await self._daemon()
        app1 = RuntimeClient(url=d.url)
        app2 = RuntimeClient(url=d.url)
        pid1 = await asyncio.to_thread(app1.submit, Biller(app="one"))
        pid2 = await asyncio.to_thread(app2.submit, Biller(app="two"))
        await asyncio.to_thread(app1.wait, pid1, 10)
        await asyncio.to_thread(app2.wait, pid2, 10)

        costs = (await asyncio.to_thread(app1.ps))["costs"]
        self.assertEqual(set(costs), {str(pid1), str(pid2)})  # JSON keys
        self.assertGreater(sum(c["cost"] for c in costs.values()), 0)

    async def test_the_daemon_outlives_its_work(self):
        """Quiescence is not an exit: late work is accepted and runs."""
        d = await self._daemon()
        client = RuntimeClient(url=d.url)
        await asyncio.to_thread(client.run, Napper(app="early"), 10)
        await asyncio.sleep(0.1)  # fully idle — an embedded kernel would have exited
        result = await asyncio.to_thread(client.run, Napper(app="late"), 10)
        self.assertEqual(result["app"], "late")

    async def test_control_via_the_client(self):
        d = await self._daemon()
        client = RuntimeClient(url=d.url)
        pid = await asyncio.to_thread(client.submit, Napper(app="doomed", nap=30))
        await asyncio.sleep(0.1)
        await asyncio.to_thread(client.kill, pid)
        with self.assertRaises(RemoteAgentFailed):
            await asyncio.to_thread(client.wait, pid, 10)

    async def test_two_mains_with_different_files_do_not_collide(self):
        """Every application is __main__ to itself; the daemon must never
        hand one application's module to another's spec."""
        d = await self._daemon()
        client = RuntimeClient(url=d.url)
        specs = []
        for tag in ("alpha", "beta"):
            file = Path(self.tmp.name) / f"app_{tag}.py"
            file.write_text(
                "import sys; from pathlib import Path\n"
                f"sys.path.insert(0, {str(Path(__file__).resolve().parents[1])!r})\n"
                "from agentos import Agent\n"
                "class Hello(Agent):\n"
                f"    async def run(self, ctx): return {tag!r}\n",
                encoding="utf-8",
            )
            specs.append(
                {"module": "__main__", "qualname": "Hello",
                 "file": str(file), "name": "Hello", "params": {}}
            )
        pids = [
            (await asyncio.to_thread(
                client._request, "POST", "/agents", {"spec": spec}
            ))["pid"]
            for spec in specs
        ]
        results = [await asyncio.to_thread(client.wait, pid, 10) for pid in pids]
        self.assertEqual(results, ["alpha", "beta"])

    async def test_health_and_logs_are_served(self):
        d = await self._daemon()
        client = RuntimeClient(url=d.url)
        health = await asyncio.to_thread(client.health)
        self.assertEqual(health["url"], d.url)
        await asyncio.to_thread(client.run, Napper(app="logged"), 10)
        logs = await asyncio.to_thread(client.logs)
        self.assertTrue(any("working for logged" in e["message"] for e in logs))


    async def test_the_dashboard_and_scheduler_state_are_served(self):
        """Phase 8: the dashboard is one GET away, and /state exposes the
        live dependency graph it draws."""
        d = await self._daemon()
        html = await asyncio.to_thread(
            lambda: urllib.request.urlopen(d.url + "/", timeout=10).read().decode("utf-8")
        )
        self.assertIn("AgentOS", html)
        self.assertIn("Dependency graph", html)

        client = RuntimeClient(url=d.url)
        await asyncio.to_thread(client.run, Napper(app="observed"), 10)
        state = await asyncio.to_thread(client._request, "GET", "/state")
        self.assertEqual(state["policy"], "fifo")
        self.assertIn("deps", state)
        self.assertEqual(state["processes"][0]["status"], "Finished")


class ProcessIsolationTest(unittest.IsolatedAsyncioTestCase):
    """The executor swap (p.17): real OS subprocesses, same kernel, same agents.

    Runs on the default transport — the loopback TCP socket. The pipe
    subclass below re-runs every test with stdio pipes carrying the syscalls;
    the tests cannot tell, which is the point.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def kernel(self, **kw):
        kw.setdefault("transport", "socket")
        return Kernel(store=self.store, tick=0.01, **kw)

    async def test_agents_run_in_a_different_address_space(self):
        result = await asyncio.wait_for(
            self.kernel().run_until_done(WhoAmI()), timeout=60
        )
        self.assertNotEqual(result["os_pid"], os.getpid())

    async def test_subprocess_agents_speak_the_full_syscall_surface(self):
        """spawn, wait, memory — from one real process into another."""
        result = await asyncio.wait_for(
            self.kernel().run_until_done(Family()), timeout=90
        )
        self.assertNotEqual(result["my_os_pid"], os.getpid())
        self.assertNotEqual(result["child_os_pid"], os.getpid())
        self.assertNotEqual(result["my_os_pid"], result["child_os_pid"])
        self.assertEqual(result["note"], "written from a real process")

    async def test_killing_a_subprocess_agent_kills_the_process(self):
        k = self.kernel()
        pid = k.spawn(Napper(app="condemned", nap=60))
        run = asyncio.create_task(k.run())

        async def until_running():
            from agentos.kernel.states import AgentState
            while k.table.get(pid).state not in (
                AgentState.RUNNING, AgentState.SLEEPING
            ):
                await asyncio.sleep(0.02)

        await asyncio.wait_for(until_running(), timeout=60)
        k.kill(pid)
        await asyncio.wait_for(run, timeout=30)
        proc = k.table.get(pid)
        self.assertEqual(proc.state.value, "Failed")
        self.assertIn("killed", proc.exit_reason)


class PipeTransportTest(ProcessIsolationTest):
    """Same guarantees when the syscall channel is stdio pipes, not TCP."""

    def kernel(self, **kw):
        kw["transport"] = "pipe"
        return super().kernel(**kw)


class SocketAuthTest(unittest.IsolatedAsyncioTestCase):
    """The socket transport's door policy: no valid token, no channel."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(self.tmp.name)
        self.addCleanup(self.store.close)

    async def test_a_connection_with_a_bogus_token_is_dropped(self):
        k = Kernel(store=self.store, tick=0.01)
        pid = k.spawn(Napper(app="sock", nap=0.1))
        run = asyncio.create_task(k.run())

        async def server_up():
            while getattr(k.executor, "_port", None) is None:
                await asyncio.sleep(0.02)

        await asyncio.wait_for(server_up(), timeout=60)
        reader, writer = await asyncio.open_connection("127.0.0.1", k.executor._port)
        writer.write(b'{"token": "bogus"}\n')
        await writer.drain()
        # The executor hangs up without ever sending a header line.
        self.assertEqual(await asyncio.wait_for(reader.readline(), timeout=30), b"")
        writer.close()
        # The impostor cost the real agent nothing.
        await asyncio.wait_for(run, timeout=60)
        self.assertEqual(k.table.get(pid).state.value, "Finished")


if __name__ == "__main__":
    unittest.main(verbosity=2)
