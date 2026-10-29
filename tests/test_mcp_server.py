"""AgentOS as an MCP server: `agent mcp` driven over stdio, as Claude Code would."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agentos import Agent  # noqa: E402
from agentos.kernel.store import Store  # noqa: E402
from agentos.runtime.daemon import Daemon  # noqa: E402

from tests._timing import LIMIT  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SERVER = str(Path(__file__).resolve().parent / "fake_mcp_server.py")


def scripted(*replies: object) -> list:
    return [{"provider": "mock", "model": "scripted", "cost_per_mtok": [1, 2],
             "script": [json.dumps(r) for r in replies]}]


MODELS = {"classes": {
    # Needs a human, then uses an MCP server it was granted, then finishes.
    "fast": scripted(
        {"action": "ask_human", "role": "Operator", "reason": "add two numbers"},
        {"action": "tool", "capability": "mcp:math", "op": "add",
         "params": {"a": 2, "b": 40}},
        {"action": "done", "result": "the sum is 42"},
    ),
}}


class Ledger(Agent):
    """In another runtime: asks this one, over MCP, what it has spent."""

    async def run(self, ctx):
        return await ctx.request_tool("mcp:agentos", "ledger")


class McpServerTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.store = Store(self.dir)
        cfg = self.dir / "math.json"
        self.log = self.dir / "calls.jsonl"
        cfg.write_text(json.dumps({"name": "math", "log": str(self.log), "tools": [{
            "name": "add", "description": "Add two numbers.",
            "inputSchema": {"type": "object", "required": ["a", "b"], "properties": {
                "a": {"type": "number"}, "b": {"type": "number"}}}}]}))
        self.daemon = Daemon(
            store=self.store, port=0, tick=0.01, models=MODELS, permissions={},
            tools={"mcp:math": {"command": [sys.executable, SERVER, str(cfg)]}},
            task_tools=["mcp:math"],
        )
        self.task = asyncio.create_task(self.daemon.start())
        await asyncio.sleep(0.15)
        self.bridge = subprocess.Popen(
            [sys.executable, "-X", "utf8", "-m", "agentos.cli", "--dir", str(self.dir), "mcp"],
            cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
            encoding="utf-8",
        )
        self.next_id = 0

    async def asyncTearDown(self):
        self.bridge.stdin.close()
        self.bridge.wait(timeout=LIMIT)
        self.bridge.stdout.close()
        self.daemon.stop()
        await asyncio.wait_for(self.task, timeout=LIMIT)
        self.store.close()

    def _rpc(self, method, params=None, notify=False):
        msg = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        if not notify:
            self.next_id += 1
            msg["id"] = self.next_id
        self.bridge.stdin.write(json.dumps(msg) + "\n")
        self.bridge.stdin.flush()
        if notify:
            return None
        return json.loads(self.bridge.stdout.readline())

    async def rpc(self, method, params=None, notify=False):
        return await asyncio.to_thread(self._rpc, method, params, notify)

    async def call(self, name, **arguments):
        reply = await self.rpc("tools/call", {"name": name, "arguments": arguments})
        return reply["result"]

    async def test_a_client_runs_a_team_end_to_end(self):
        init = await self.rpc("initialize", {"protocolVersion": "2025-06-18",
                                             "capabilities": {},
                                             "clientInfo": {"name": "test", "version": "0"}})
        self.assertEqual(init["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual(init["result"]["serverInfo"]["name"], "agentos")
        await self.rpc("notifications/initialized", notify=True)

        listed = await self.rpc("tools/list")
        self.assertEqual({t["name"] for t in listed["result"]["tools"]},
                         {"submit_task", "task_status", "ps", "approve", "ledger"})

        sent = await self.call("submit_task", goal="add 2 and 40", tools=["mcp:math"])
        self.assertFalse(sent["isError"], sent)
        pid = sent["structuredContent"]["pid"]
        self.assertEqual(sent["structuredContent"]["granted"], ["mcp:math"])

        async def blocked():
            while True:
                rows = (await self.call("ps"))["structuredContent"]["processes"]
                row = next(r for r in rows if r["pid"] == pid)
                if row["status"] == "Blocked":
                    return row
                await asyncio.sleep(0.05)

        row = await asyncio.wait_for(blocked(), LIMIT)
        self.assertEqual(row["permissions"], ["mcp:math"])
        self.assertFalse(self.log.exists(), "nothing ran before the approval")

        approved = await self.call("approve", pid=pid, role="Operator")
        self.assertFalse(approved["isError"], approved)

        status = await self.call("task_status", pid=pid, wait_seconds=LIMIT)
        self.assertEqual(status["structuredContent"]["status"], "Finished")
        self.assertEqual(status["structuredContent"]["result"], "the sum is 42")
        calls = [json.loads(line) for line in self.log.read_text().splitlines()]
        self.assertEqual(calls[0]["args"], {"a": 2, "b": 40},
                         "the team reached the MCP server through the kernel")

        ledger = (await self.call("ledger"))["structuredContent"]
        self.assertGreater(ledger["total_usd"], 0)
        self.assertEqual(ledger["by_tool"]["mcp:math"]["calls"], 1)

    def meta(self, version="2026-07-28", capabilities=True):
        m = {"io.modelcontextprotocol/protocolVersion": version,
             "io.modelcontextprotocol/clientInfo": {"name": "test", "version": "0"}}
        if capabilities:
            m["io.modelcontextprotocol/clientCapabilities"] = {}
        return {"_meta": m}

    async def test_a_stateless_client_needs_no_handshake(self):
        found = await self.rpc("server/discover", self.meta())
        self.assertIn("2026-07-28", found["result"]["supportedVersions"])
        self.assertEqual(found["result"]["resultType"], "complete")
        out = await self.rpc("tools/call", {"name": "ps", "arguments": {}, **self.meta()})
        self.assertEqual(out["result"]["resultType"], "complete")
        self.assertFalse(out["result"]["isError"], out)
        self.assertEqual(out["result"]["_meta"]["io.modelcontextprotocol/serverInfo"]["name"],
                         "agentos")

    async def test_a_version_we_do_not_speak_is_answered_with_the_ones_we_do(self):
        out = await self.rpc("tools/list", self.meta(version="2099-01-01"))
        self.assertEqual(out["error"]["code"], -32022)
        self.assertIn("2026-07-28", out["error"]["data"]["supported"])
        out = await self.rpc("tools/list", self.meta(capabilities=False))
        self.assertEqual(out["error"]["code"], -32602)

    async def test_another_agentos_mounts_this_one_as_an_mcp_server(self):
        """Both directions at once: a runtime's MCP driver talking to `agent mcp`."""
        from agentos import Kernel

        other = tempfile.TemporaryDirectory()
        self.addCleanup(other.cleanup)
        store = Store(Path(other.name) / "rt")
        self.addCleanup(store.close)
        bridge = [sys.executable, "-X", "utf8", "-m", "agentos.cli",
                  "--dir", str(self.dir), "mcp"]
        k = Kernel(store=store, tick=0.01, permissions={"*": ["mcp:agentos"]},
                   tools={"mcp:agentos": {"command": bridge, "cwd": str(ROOT)}})
        out = await asyncio.wait_for(k.run_until_done(Ledger()), LIMIT)
        self.assertIn("total_usd", out)
        self.assertTrue(k._drivers["mcp:agentos"].modern, "they agreed on 2026-07-28")

    async def test_a_capability_the_daemon_does_not_allow_is_refused(self):
        out = await self.call("submit_task", goal="anything", tools=["shell"])
        self.assertTrue(out["isError"])
        self.assertIn("does not allow shell", out["content"][0]["text"])

    async def test_bad_arguments_and_unknown_tools_come_back_as_tool_errors(self):
        out = await self.call("approve", pid=1)
        self.assertTrue(out["isError"])
        self.assertIn("role", out["content"][0]["text"])
        out = await self.call("rm_rf")
        self.assertTrue(out["isError"])
        unknown = await self.rpc("resources/list")
        self.assertEqual(unknown["error"]["code"], -32601)

    async def test_no_daemon_is_a_readable_error_not_a_crash(self):
        self.daemon.stop()
        await asyncio.wait_for(self.task, timeout=LIMIT)
        self.task = asyncio.create_task(asyncio.sleep(0))
        out = await self.call("ps")
        self.assertTrue(out["isError"])
        self.assertIn("agent daemon", out["content"][0]["text"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
