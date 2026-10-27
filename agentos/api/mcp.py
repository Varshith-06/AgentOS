"""AgentOS as an MCP server: an MCP client (Claude Code, say) runs teams here.

    agent mcp                      # stdio; talks to the daemon in ./.agentos
    claude mcp add agentos -- agent mcp --dir /path/to/.agentos

A thin bridge, like the CLI: every tool is one call to the daemon's HTTP API,
under the daemon's token, so a task submitted here gets the same ceiling,
budget, journal and ledger as one POSTed to /task. Stdio, newline-delimited
JSON-RPC 2.0, stdlib only. Both protocol eras: stateless 2026-07-28 clients,
and older ones that open with initialize.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any, Callable

from .. import __version__
from ..client import DaemonUnavailable, RuntimeClient

MODERN = ("2026-07-28",)  # stateless: the version rides on every request
LEGACY = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")  # initialize first
VERSION_KEY = "io.modelcontextprotocol/protocolVersion"
CAPABILITIES_KEY = "io.modelcontextprotocol/clientCapabilities"
SERVER_INFO = {"name": "agentos", "title": "AgentOS", "version": __version__}
INSTRUCTIONS = (
    "AgentOS runs teams of agents as OS processes under a kernel that checks "
    "every tool call against what was granted. submit_task starts one; "
    "task_status follows it; approve unblocks an agent waiting on a human; "
    "ledger says what everything cost."
)
TERMINAL = ("Finished", "Failed")
LONGEST_WAIT = 50.0  # seconds; MCP clients time out a call around a minute

TOOLS: list[dict[str, Any]] = [
    {
        "name": "submit_task",
        "title": "Submit a task to AgentOS",
        "description": (
            "Hand AgentOS a goal in plain words. A planner invents a team of "
            "agents to do it, using only the tools you list, and the daemon's "
            "ceiling caps those. Returns the task's pid at once; follow it with "
            "task_status."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "goal": {"type": "string", "description": "What the team should achieve."},
                "tools": {"type": "array", "items": {"type": "string"},
                          "description": "Capabilities the team may use, e.g. "
                                         "[\"filesystem\"]. Only those the daemon "
                                         "allows for submitted tasks are granted."},
                "budget_usd": {"type": "number",
                               "description": "Most the whole team may spend on models."},
                "max_steps": {"type": "integer", "description": "Model calls per agent."},
            },
            "required": ["goal"],
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False,
                        "idempotentHint": False, "openWorldHint": True},
    },
    {
        "name": "task_status",
        "title": "A task's status and result",
        "description": (
            "The task's status, its result once finished, and every agent the "
            "planner created. With wait_seconds, waits up to that long for the "
            "task to finish first."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "pid": {"type": "integer"},
                "wait_seconds": {"type": "number",
                                 "description": f"0 to {LONGEST_WAIT:.0f}; default 0."},
            },
            "required": ["pid"],
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "ps",
        "title": "Every agent in the runtime",
        "description": (
            "The process table: each agent's pid, name, parent, status, what it "
            "waits on (an approval shows here), and the capabilities it holds."
        ),
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "approve",
        "title": "Approve an agent's pending request",
        "description": (
            "Grant the human approval an agent is blocked on. ps shows who waits "
            "and for which role. Call this only when the user has said to approve "
            "that request: the approval stands in for a person."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "pid": {"type": "integer"},
                "role": {"type": "string", "description": "The role the agent asked for."},
            },
            "required": ["pid", "role"],
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False,
                        "idempotentHint": True, "openWorldHint": False},
    },
    {
        "name": "ledger",
        "title": "What the runtime has spent",
        "description": (
            "Model spending by agent and by model, exact to the token, and tool "
            "calls by capability, across every application using this runtime."
        ),
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
]


class Bridge:
    """The tools, each a call to the daemon."""

    def __init__(self, dirpath: str | Path = ".agentos") -> None:
        self.dirpath = dirpath

    def client(self) -> RuntimeClient:
        # Found per call: the daemon may have restarted on another port.
        return RuntimeClient(dirpath=self.dirpath)

    def submit_task(self, goal: str, tools: list[str] | None = None,
                    budget_usd: float | None = None, max_steps: int | None = None) -> dict:
        body: dict[str, Any] = {"goal": goal, "tools": list(tools or [])}
        if budget_usd is not None:
            body["budget_usd"] = budget_usd
        if max_steps is not None:
            body["max_steps"] = max_steps
        sent = self.client()._request("POST", "/task", body)
        return {"pid": sent["pid"], "granted": sent["granted"],
                "budget_usd": sent["budget_usd"],
                "next": f"task_status with pid {sent['pid']}"}

    def task_status(self, pid: int, wait_seconds: float = 0) -> dict:
        c = self.client()
        deadline = time.monotonic() + max(0.0, min(float(wait_seconds), LONGEST_WAIT))
        while True:
            tree = c.task_tree(pid)
            if tree.get("status") in TERMINAL or time.monotonic() >= deadline:
                break
            time.sleep(0.5)
        keep = ("pid", "name", "parent", "status", "waiting_on", "permissions", "result")
        return {**tree, "agents": [{k: r.get(k) for k in keep} for r in tree["agents"]]}

    def ps(self) -> dict:
        table = self.client().ps()
        keep = ("pid", "name", "parent", "status", "waiting_on", "permissions", "priority")
        return {"processes": [{k: r.get(k) for k in keep} for r in table["processes"]]}

    def approve(self, pid: int, role: str) -> dict:
        self.client().approve(pid, role)
        return {"approved": pid, "role": role}

    def ledger(self) -> dict:
        table = self.client().ps()
        costs = table.get("costs") or {}
        return {
            "total_usd": round(sum(c["cost"] for c in costs.values()), 6),
            "by_agent": costs,
            "by_model": table.get("models") or {},
            "by_tool": table.get("tools") or {},
        }


def handle(bridge: Bridge, msg: dict[str, Any]) -> dict[str, Any] | None:
    """One JSON-RPC message in, the reply out (None for a notification).

    Both eras: a request whose _meta names a protocol version is served
    statelessly (2026-07-28); an initialize opens the older handshake."""
    method, rid = msg.get("method"), msg.get("id")
    if rid is None:
        return None
    params = msg.get("params") or {}
    meta = params.get("_meta") or {}
    asked = meta.get(VERSION_KEY)
    modern = asked is not None and method != "initialize"
    if modern and asked not in MODERN:
        return _rpc_error(rid, -32022, "Unsupported protocol version",
                          {"supported": [*MODERN, *LEGACY], "requested": asked})
    if (modern and CAPABILITIES_KEY not in meta) or (method == "server/discover" and not modern):
        return _rpc_error(rid, -32602, f"_meta must carry {VERSION_KEY} and {CAPABILITIES_KEY}")

    if method == "server/discover":
        result: dict[str, Any] = {"supportedVersions": [*MODERN, *LEGACY],
                                  "capabilities": {"tools": {}},
                                  "instructions": INSTRUCTIONS}
    elif method == "initialize":
        offered = params.get("protocolVersion")
        result = {
            "protocolVersion": offered if offered in LEGACY else LEGACY[0],
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": SERVER_INFO,
            "instructions": INSTRUCTIONS,
        }
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        result = call_tool(bridge, params.get("name"), params.get("arguments") or {})
    else:
        return _rpc_error(rid, -32601, f"method not found: {method}")
    if modern:
        result = {"resultType": "complete", **result,
                  "_meta": {"io.modelcontextprotocol/serverInfo": SERVER_INFO}}
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def _rpc_error(rid: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": rid, "error": error}


def call_tool(bridge: Bridge, name: Any, args: dict[str, Any]) -> dict[str, Any]:
    fn: Callable[..., dict] | None = (getattr(bridge, name, None)
                                      if name in {t["name"] for t in TOOLS} else None)
    if fn is None:
        return _error(f"no tool {name!r}; tools: {', '.join(t['name'] for t in TOOLS)}")
    try:
        value = fn(**args)
    except TypeError as exc:
        return _error(f"bad arguments for {name}: {exc}")
    except DaemonUnavailable as exc:
        return _error(f"{exc}. Start the runtime first: agent daemon")
    except Exception as exc:
        return _error(f"{type(exc).__name__}: {exc}")
    return {"content": [{"type": "text", "text": json.dumps(value, default=str)}],
            "structuredContent": value, "isError": False}


def _error(text: str) -> dict[str, Any]:
    # A tool that fails says so in its result, where the model can read it.
    return {"content": [{"type": "text", "text": text}], "isError": True}


def serve(dirpath: str | Path = ".agentos", stdin=None, stdout=None) -> int:
    """Read requests from stdin until it closes; answer on stdout."""
    if stdin is None:
        stdin = sys.stdin
        stdin.reconfigure(encoding="utf-8")
    if stdout is None:
        stdout = sys.stdout
        # One message per line, and a line ends in "\n" even on Windows.
        stdout.reconfigure(encoding="utf-8", newline="\n")
    bridge = Bridge(dirpath)
    for line in stdin:
        try:
            msg = json.loads(line)
        except ValueError:
            reply: dict[str, Any] | None = {
                "jsonrpc": "2.0", "id": None,
                "error": {"code": -32700, "message": "parse error"}}
        else:
            reply = handle(bridge, msg) if isinstance(msg, dict) else None
        if reply is not None:
            stdout.write(json.dumps(reply) + "\n")
            stdout.flush()
    return 0
