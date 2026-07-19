"""Phase 4: permissions and tool drivers.

The bar (AgentOS.pdf p.15): an agent cannot reach a tool it was not granted —
the kernel validates the capability before dispatch, the denial lands in the
audit log, and revoking a capability in config changes behaviour with no code
edit, even on a running system.
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agentos import Agent, Kernel, KernelError, ToolDriver, Transient  # noqa: E402
from agentos.drivers import REGISTRY  # noqa: E402
from agentos.kernel.states import AgentState  # noqa: E402
from agentos.kernel.store import Store  # noqa: E402


class TwoTicks(Agent):
    """Two calls to a rate-limited driver; returns the gap between them."""

    async def run(self, ctx):
        first = await ctx.request_tool("metronome", "tick")
        second = await ctx.request_tool("metronome", "tick")
        return second - first


class ToolUser(Agent):
    """Makes one tool call and reports what happened, denial included."""

    async def run(self, ctx):
        try:
            value = await ctx.request_tool(
                self.params["capability"],
                self.params["op"],
                **self.params.get("args", {}),
            )
        except KernelError as exc:
            return {"denied": str(exc), "value": None}
        return {"denied": None, "value": value}


class Finance(Agent):
    """The p.7 matrix: sql is granted, browser is not."""

    async def run(self, ctx):
        rows = await ctx.request_tool("sql", "query", query="SELECT 40 + 2 AS answer")
        try:
            await ctx.request_tool("browser", "open", url="https://example.invalid/")
            browser = "allowed"
        except KernelError:
            browser = "denied"
        return {"answer": rows[0]["answer"], "browser": browser}


class FsAgent(Agent):
    async def run(self, ctx):
        await ctx.request_tool(
            "filesystem", "write", path="notes/a.txt", content="hello"
        )
        text = await ctx.request_tool("filesystem", "read", path="notes/a.txt")
        try:
            await ctx.request_tool("filesystem", "read", path="../outside.txt")
            escape = "allowed"
        except KernelError as exc:
            escape = str(exc)
        return {"text": text, "escape": escape}


class TwoCalls(Agent):
    """Calls a tool, sleeps, calls it again — long enough to be revoked between."""

    async def run(self, ctx):
        await ctx.request_tool("python", "run", code="print(1)")
        await ctx.sleep(self.params.get("gap", 0.3))
        try:
            await ctx.request_tool("python", "run", code="print(2)")
        except KernelError:
            return "revoked mid-run"
        return "still allowed"


class Flaky(ToolDriver):
    """Fails twice, then succeeds: exercises the base driver's retry loop."""

    name = "flaky"
    retries = 3

    def __init__(self, **kw):
        super().__init__(**kw)
        self.calls = 0

    async def op_go(self):
        self.calls += 1
        if self.calls < 3:
            raise Transient("hiccup")
        return f"ok after {self.calls} attempts"


class Metronome(ToolDriver):
    """Reports when each call actually ran, to observe the rate limiter."""

    name = "metronome"
    min_interval = 0.15

    async def op_tick(self):
        return time.monotonic()


class Rude(ToolDriver):
    """Returns something that cannot cross the message boundary."""

    name = "rude"

    async def op_leak(self):
        return object()


class ToolTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def kernel(self, **kw):
        return Kernel(store=self.store, tick=0.01, **kw)

    def register(self, name, cls):
        REGISTRY[name] = cls
        self.addCleanup(REGISTRY.pop, name, None)

    async def _until(self, predicate, timeout=5.0):
        async def poll():
            while not predicate():
                await asyncio.sleep(0.01)

        await asyncio.wait_for(poll(), timeout)

    # -- the permission matrix (p.7) ---------------------------------------
    async def test_an_ungranted_capability_is_denied_and_audited(self):
        k = self.kernel(permissions={})
        result = await asyncio.wait_for(
            k.run_until_done(
                ToolUser(capability="sql", op="query", args={"query": "SELECT 1"})
            ),
            timeout=5,
        )
        self.assertIn("does not hold capability 'sql'", result["denied"])
        self.assertTrue(
            any(e["kind"] == "denied" for e in self.store.logs()),
            "the denial must be in the audit log",
        )

    async def test_the_p7_matrix_sql_yes_browser_no(self):
        k = self.kernel(permissions={"Finance": ["sql"]})
        result = await asyncio.wait_for(k.run_until_done(Finance()), timeout=5)
        self.assertEqual(result, {"answer": 42, "browser": "denied"})

    async def test_wildcard_grants(self):
        k = self.kernel(permissions={"*": ["sql"]})
        result = await asyncio.wait_for(
            k.run_until_done(
                ToolUser(capability="sql", op="query", args={"query": "SELECT 1 AS x"})
            ),
            timeout=5,
        )
        self.assertIsNone(result["denied"])

    async def test_revocation_applies_to_a_running_system(self):
        """No code edit: the config file changes, the next call is refused."""
        perms_path = Path(self.tmp.name) / "permissions.json"
        perms_path.write_text(json.dumps({"TwoCalls": ["python"]}))
        k = self.kernel(permissions=str(perms_path))
        pid = k.spawn(TwoCalls())
        run = asyncio.create_task(k.run())
        await self._until(
            lambda: any(e["kind"] == "tool" for e in self.store.logs())
        )  # the first call went through
        perms_path.write_text(json.dumps({}))  # the human revokes it, mid-run
        await asyncio.wait_for(run, timeout=10)
        self.assertEqual(k.table.get(pid).result, "revoked mid-run")

    async def test_unknown_capability_is_an_error_not_a_crash(self):
        k = self.kernel(permissions={"ToolUser": ["teleport"]})
        result = await asyncio.wait_for(
            k.run_until_done(ToolUser(capability="teleport", op="go")), timeout=5
        )
        self.assertIn("no driver for capability 'teleport'", result["denied"])

    # -- the drivers ---------------------------------------------------------
    async def test_sql_driver_round_trip(self):
        k = self.kernel(permissions={"ToolUser": ["sql"]})
        result = await asyncio.wait_for(
            k.run_until_done(
                ToolUser(
                    capability="sql",
                    op="query",
                    args={"query": "SELECT ? + ? AS total", "params": [40, 2]},
                )
            ),
            timeout=5,
        )
        self.assertEqual(result["value"], [{"total": 42}])

    async def test_filesystem_is_sandboxed_and_announces_new_files(self):
        with tempfile.TemporaryDirectory() as root:
            k = self.kernel(
                permissions={"FsAgent": ["filesystem"]},
                tools={"filesystem": {"root": root}},
            )
            result = await asyncio.wait_for(k.run_until_done(FsAgent()), timeout=5)
        self.assertEqual(result["text"], "hello")
        self.assertIn("escapes the sandbox root", result["escape"])
        self.assertIn("FileCreated", [e.type for e in k.bus.history])  # p.5

    async def test_shell_and_python_drivers_run_commands(self):
        k = self.kernel(permissions={"ToolUser": ["shell", "python"]})
        shell = await asyncio.wait_for(
            k.run_until_done(
                ToolUser(capability="shell", op="run", args={"command": "echo hi"})
            ),
            timeout=15,
        )
        self.assertIn("hi", shell["value"]["stdout"])
        self.assertEqual(shell["value"]["returncode"], 0)

        k2 = self.kernel(permissions={"ToolUser": ["python"]})
        py = await asyncio.wait_for(
            k2.run_until_done(
                ToolUser(capability="python", op="run", args={"code": "print(6 * 7)"})
            ),
            timeout=15,
        )
        self.assertEqual(py["value"]["stdout"].strip(), "42")

    # -- tool calls are scheduling, not function calls ------------------------
    async def test_a_tool_call_is_a_scheduler_wait_not_a_hang(self):
        """The agent visibly Waits on the tool, and a running tool is not a
        deadlock even when every agent in the system is waiting on it."""
        k = self.kernel(permissions={"ToolUser": ["python"]})
        pid = k.spawn(
            ToolUser(
                capability="python",
                op="run",
                args={"code": "import time; time.sleep(0.6)"},
            )
        )
        run = asyncio.create_task(k.run())
        await self._until(
            lambda: k.table.get(pid).state is AgentState.WAITING
            and k.table.get(pid).waiting_on == "tool python"
        )
        await asyncio.wait_for(run, timeout=15)  # would deadlock-fail if broken
        self.assertIs(k.table.get(pid).state, AgentState.FINISHED)
        self.assertIn("ToolCompleted", [e.type for e in k.bus.history])

    # -- what the base driver owns: retries, rate limits, the boundary --------
    async def test_transient_failures_are_retried(self):
        self.register("flaky", Flaky)
        k = self.kernel(permissions={"ToolUser": ["flaky"]})
        result = await asyncio.wait_for(
            k.run_until_done(ToolUser(capability="flaky", op="go")), timeout=5
        )
        self.assertEqual(result["value"], "ok after 3 attempts")

    async def test_rate_limit_spaces_out_calls(self):
        self.register("metronome", Metronome)

        k = self.kernel(permissions={"TwoTicks": ["metronome"]})
        gap = await asyncio.wait_for(k.run_until_done(TwoTicks()), timeout=5)
        self.assertGreaterEqual(gap, 0.1)

    async def test_a_non_serializable_tool_result_is_refused(self):
        """Agents may only receive plain data — even from a trusted driver."""
        self.register("rude", Rude)
        k = self.kernel(permissions={"ToolUser": ["rude"]})
        result = await asyncio.wait_for(
            k.run_until_done(ToolUser(capability="rude", op="leak")), timeout=5
        )
        self.assertIn("not JSON-serializable", result["denied"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
