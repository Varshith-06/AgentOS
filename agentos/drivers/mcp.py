"""The MCP driver: a Model Context Protocol server, mounted as a capability.

    Kernel(tools={"mcp:github": {"command": ["npx", "-y", "@modelcontextprotocol/server-github"],
                                 "env": {"GITHUB_TOKEN": "..."}}})

Every tool the server lists becomes an op of the capability `mcp:github`, so
an MCP call is permission-checked, attenuated down the agent tree, journaled,
and flagged OutcomeAmbiguous after a crash, like any other tool call. The
transport is stdio: newline-delimited JSON-RPC 2.0, stdlib only. Both eras of
the protocol: a stateless 2026-07-28 server (found by server/discover), and an
older one that wants the initialize handshake.

A server is code the operator did not write, and its tool descriptions go
straight into the model's prompt. Three defences sit here:

- Pinning. Each tool definition is hashed the first time the server is
  mounted, and the hash is kept in the runtime's state directory. A tool whose
  definition later changes, or a tool that appears after the pin, is refused
  (a "rug pull"). Delete the server's entry in mcp_pins.json to re-pin, or
  set "pin": false for a server whose tools change by design.
- Quarantine. A definition carrying instructions aimed at the model (tool
  poisoning) is withheld from the prompt and refused at call time, unless the
  operator lists the tool under "allow". This is a pattern check and can be
  evaded; the capability ceiling is what holds when it is.
- No sampling. A server may not ask the runtime for model completions, so it
  cannot spend the task's budget or talk to the model behind the kernel's back.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import unicodedata
from pathlib import Path
from typing import Any

from .base import ToolDriver, ToolError, Transient

MODERN = "2026-07-28"  # stateless: every request carries its version in _meta
LEGACY = "2025-11-25"  # the last revision with an initialize handshake
PROBE_TIMEOUT = 10.0   # a legacy server may not answer server/discover at all
UNSUPPORTED_VERSION = -32022
LINE_LIMIT = 16 * 1024 * 1024  # one JSON-RPC message per line, and lists get long

# Instructions aimed at the model rather than a description of the tool.
# Each is narrow on purpose: a description may say "password" or "never more
# than 10 results" and be honest. What these match, an honest tool never needs.
POISON = [
    (re.compile(r"<\s*/?\s*(important|system|instructions?|secret|hidden)\b", re.I),
     "a tag addressed to the model"),
    (re.compile(r"\b(ignore|disregard|override|forget)\s+(all\s+|any\s+)?(previous|prior|"
                r"above|earlier|your|other|the system)\b.{0,20}\b(instructions?|constraints?"
                r"|rules?|prompts?)\b", re.I | re.S),
     "an instruction to ignore instructions"),
    (re.compile(r"\b(do not|don't|never)\b.{0,30}\b(tell|mention|inform|reveal|notify)\b"
                r".{0,20}\buser", re.I | re.S),
     "an instruction to hide something from the user"),
    (re.compile(r"~/\.ssh|\bid_rsa\b|\bid_ed25519\b|\.aws/credentials|(?<![\w.])\.env\b"
                r"|\bmcp\.json\b|\.cursor/|\.claude/", re.I),
     "a path to someone's secrets"),
]
HIDDEN = {"Cf", "Co"}  # format (zero-width, bidi, tag) and private-use characters


class RpcError(ToolError):
    """The server answered with a JSON-RPC error."""

    def __init__(self, message: str, code: Any = None, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.data = data


def fingerprint(tool: dict[str, Any]) -> str:
    """What the model and the kernel rely on, hashed. Anything else may change."""
    kept = {k: tool.get(k) for k in
            ("name", "title", "description", "inputSchema", "outputSchema", "annotations")}
    blob = json.dumps(kept, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _texts(node: Any) -> list[str]:
    """Every string a model could read in a definition: descriptions, titles,
    enum values, defaults, at any depth of the schema."""
    if isinstance(node, str):
        return [node]
    if isinstance(node, dict):
        return [t for k, v in node.items() for t in _texts(k) + _texts(v)]
    if isinstance(node, list):
        return [t for v in node for t in _texts(v)]
    return []


def poisoning(tool: dict[str, Any]) -> list[str]:
    """Why this definition looks like it is talking to the model. Empty if clean."""
    text = "\n".join(_texts({k: tool.get(k) for k in
                             ("title", "description", "inputSchema", "annotations")}))
    found = [why for pattern, why in POISON if pattern.search(text)]
    hidden = sorted({f"U+{ord(c):04X}" for c in text
                     if unicodedata.category(c) in HIDDEN})
    if hidden:
        found.append(f"invisible characters ({', '.join(hidden[:4])})")
    return found


class McpDriver(ToolDriver):
    name = "mcp"
    timeout = 60.0

    def __init__(
        self,
        command: list[str] | None = None,
        *,
        server: str = "mcp",
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        pins: dict[str, str] | None = None,
        pins_path: str | Path | None = None,
        allow: list[str] | None = None,
        pin: bool = True,
        **kw: Any,
    ) -> None:
        super().__init__(**kw)
        # pin=False trusts each listing as it comes: for a server whose tools
        # change by design, at the price of the rug-pull check.
        self.pin = pin
        if not command or not isinstance(command, list):
            raise ToolError(
                f"mcp:{server} needs a command, e.g. "
                '{"command": ["python", "my_server.py"]}'
            )
        self.server = server
        self.name = f"mcp:{server}"
        self.command = [str(c) for c in command]
        self.env = env or {}
        self.cwd = cwd
        self.allow = set(allow or ())
        self.pins_path = Path(pins_path) if pins_path is not None else None
        # Pins given in config are the operator's; otherwise the first listing's.
        self._pins: dict[str, str] | None = dict(pins) if pins else self._load_pins()
        self._proc: asyncio.subprocess.Process | None = None
        self._reader: asyncio.Task | None = None
        self._pending: dict[int, asyncio.Future] = {}
        self._next_id = 0
        self._start_lock: asyncio.Lock | None = None
        self._tools: dict[str, dict[str, Any]] = {}
        self._refused: dict[str, str] = {}
        self._flagged: dict[str, str] = {}  # refused by the kernel (shadowing)
        self._stale = True
        self.modern = False  # decided per server process, by _discover
        self.server_info: dict[str, Any] = {}

    # -- pins -------------------------------------------------------------

    def _load_pins(self) -> dict[str, str] | None:
        if self.pins_path is None or not self.pins_path.exists():
            return None
        try:
            return json.loads(self.pins_path.read_text(encoding="utf-8")).get(self.server)
        except (ValueError, AttributeError):
            return None

    def _save_pins(self) -> None:
        if self.pins_path is None or self._pins is None:
            return
        try:
            data = json.loads(self.pins_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        data[self.server] = self._pins
        self.pins_path.parent.mkdir(parents=True, exist_ok=True)
        self.pins_path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n",
                                  encoding="utf-8")

    # -- transport --------------------------------------------------------

    async def _ensure(self) -> None:
        if self._start_lock is None:
            self._start_lock = asyncio.Lock()
        async with self._start_lock:
            if self._proc is not None and self._proc.returncode is None:
                return
            # On Windows `npx` is npx.cmd; a bare name will not start without this.
            exe = shutil.which(self.command[0], path=self.env.get("PATH")) or self.command[0]
            try:
                self._proc = await asyncio.create_subprocess_exec(
                    exe, *self.command[1:],
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                    env={**os.environ, **self.env},
                    cwd=self.cwd,
                    limit=LINE_LIMIT,
                )
            except OSError as exc:
                raise ToolError(f"{self.name}: cannot start {self.command[0]!r}: {exc}") from exc
            self._reader = asyncio.create_task(self._read(self._proc))
            self.modern = False
            self._stale = True
            if await self._discover():
                self.modern = True
                version = MODERN
            else:
                init = await self._rpc("initialize", {
                    "protocolVersion": LEGACY,
                    "capabilities": {},  # no sampling, no roots, no elicitation
                    "clientInfo": {"name": "agentos", "version": _version()},
                })
                self.server_info = init.get("serverInfo") or {}
                version = init.get("protocolVersion", "?")
                await self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
            self._log(f"{self.name}: connected to {self.server_info.get('name', '?')} "
                      f"(protocol {version})")

    async def _discover(self) -> bool:
        """Probe with server/discover, as the spec's stdio rules say: True for a
        modern server we can speak to, False for a legacy one (any other error,
        or silence), and a refusal for a modern server we cannot speak to."""
        try:
            found = await self._rpc("server/discover", {}, timeout=PROBE_TIMEOUT, meta=True)
            versions = found.get("supportedVersions") or []
            self.server_info = (found.get("_meta") or {}).get(
                "io.modelcontextprotocol/serverInfo") or {}
        except RpcError as exc:
            if exc.code != UNSUPPORTED_VERSION:
                return False
            versions = (exc.data or {}).get("supported") or []
        except TimeoutError:
            return False
        if MODERN in versions:
            return True
        if any(v <= LEGACY for v in versions):
            return False  # it speaks a handshake version too
        raise ToolError(f"{self.name}: the server speaks only {', '.join(versions) or '?'}; "
                        f"AgentOS speaks {MODERN} and the handshake versions before it")

    def _meta(self) -> dict[str, Any]:
        return {"io.modelcontextprotocol/protocolVersion": MODERN,
                "io.modelcontextprotocol/clientInfo": {"name": "agentos", "version": _version()},
                "io.modelcontextprotocol/clientCapabilities": {}}

    async def _send(self, message: dict[str, Any]) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None:
            raise Transient(f"{self.name}: server is not running")
        try:
            proc.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise Transient(f"{self.name}: server closed its input") from exc

    async def _rpc(self, method: str, params: dict[str, Any], timeout: float | None = None,
                   meta: bool | None = None) -> dict[str, Any]:
        """One request. A modern server is stateless: every request says which
        version it speaks and what the client can do."""
        if meta if meta is not None else self.modern:
            params = {**params, "_meta": {**params.get("_meta", {}), **self._meta()}}
        self._next_id += 1
        rid = self._next_id
        future = asyncio.get_running_loop().create_future()
        self._pending[rid] = future
        try:
            await self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
            msg = await asyncio.wait_for(future, timeout or self.timeout)
        finally:
            self._pending.pop(rid, None)
        if "error" in msg:
            err = msg["error"] if isinstance(msg["error"], dict) else {}
            raise RpcError(f"{self.name}: {method} failed: {err.get('message', msg['error'])}",
                           err.get("code"), err.get("data"))
        result = msg.get("result") or {}
        kind = result.get("resultType", "complete")  # absent: a legacy server
        if kind == "input_required":
            # Elicitation, sampling, roots: a server may not reach past the kernel.
            raise ToolError(f"{self.name}: {method} asked the client for input "
                            f"({', '.join(result.get('inputRequests') or {}) or '?'}); "
                            "AgentOS does not offer servers input")
        if kind != "complete":
            raise ToolError(f"{self.name}: {method} returned an unknown resultType {kind!r}")
        return result

    async def _read(self, proc: asyncio.subprocess.Process) -> None:
        assert proc.stdout is not None
        while True:
            try:
                line = await proc.stdout.readline()
            except (ValueError, OSError):
                line = b""
            if not line:
                break
            try:
                msg = json.loads(line)
            except ValueError:
                continue  # a server that prints to stdout; not ours to parse
            if not isinstance(msg, dict):
                continue
            if "method" in msg:
                await self._from_server(msg)
            elif msg.get("id") in self._pending:
                future = self._pending[msg["id"]]
                if not future.done():
                    future.set_result(msg)
        if self._proc is proc:
            self._proc = None  # the next call starts it again
        for future in self._pending.values():
            if not future.done():
                future.set_exception(Transient(f"{self.name}: server exited"))

    async def _from_server(self, msg: dict[str, Any]) -> None:
        method = msg.get("method")
        if method == "notifications/tools/list_changed":
            self._stale = True
            self._log(f"{self.name}: server says its tools changed; re-checking pins")
            return
        if "id" not in msg:
            return  # other notifications: progress, logging, ...
        if method == "ping":
            reply: dict[str, Any] = {"result": {}}
        else:
            # Sampling, roots, elicitation: a server may not reach past the kernel.
            reply = {"error": {"code": -32601, "message": f"{method} is not offered"}}
        await self._send({"jsonrpc": "2.0", "id": msg["id"], **reply})

    async def close(self) -> None:
        """Close its input and give it a moment to exit, then end it."""
        proc, self._proc = self._proc, None
        if proc is not None and proc.returncode is None:
            try:
                if proc.stdin is not None:
                    proc.stdin.close()
                await asyncio.wait_for(proc.wait(), 2.0)
            except (TimeoutError, OSError):
                proc.kill()
                await proc.wait()
        if self._reader is not None:
            self._reader.cancel()

    # -- tools ------------------------------------------------------------

    async def _refresh(self, force: bool = False) -> None:
        """List the server's tools and hold each one to its pin. With pins on,
        every call and every description bound for a prompt is checked against
        a fresh listing; without, a call re-lists only when the server says
        its tools changed."""
        await self._ensure()
        if not (self._stale or force):
            return
        listed: list[dict[str, Any]] = []
        cursor = None
        while True:
            page = await self._rpc("tools/list", {"cursor": cursor} if cursor else {})
            listed += [t for t in page.get("tools") or [] if isinstance(t, dict) and t.get("name")]
            cursor = page.get("nextCursor")
            if not cursor:
                break
        self._stale = False
        if self.pin and self._pins is None:
            self._pins = {t["name"]: fingerprint(t) for t in listed}
            self._save_pins()
            self._log(f"{self.name}: pinned {len(listed)} tool definition(s)")
        self._tools = {t["name"]: t for t in listed}
        refused: dict[str, str] = {}
        for name, tool in self._tools.items():
            pin = self._pins.get(name) if self.pin else fingerprint(tool)
            if pin is None:
                refused[name] = "it appeared after the server's tools were pinned"
            elif pin != fingerprint(tool):
                refused[name] = "its definition changed after it was pinned"
            elif name not in self.allow and (why := poisoning(tool)):
                refused[name] = "its description carries " + "; ".join(why)
            elif name in self._flagged:
                refused[name] = self._flagged[name]
        for name, why in refused.items():
            if self._refused.get(name) != why:
                self._log(f"{self.name}.{name} refused: {why}")
                self._publish("McpToolRefused", server=self.server, tool=name, reason=why)
        self._refused = refused

    def refuse(self, tool: str, why: str) -> None:
        """The kernel found something about this tool the driver alone could not."""
        self._flagged[tool] = why
        if self._refused.get(tool) != why:
            self._refused[tool] = why
            self._log(f"{self.name}.{tool} refused: {why}")
            self._publish("McpToolRefused", server=self.server, tool=tool, reason=why)

    def ops(self) -> list[str]:
        return sorted(n for n in self._tools if n not in self._refused)

    async def describe(self) -> dict[str, Any]:
        await self._refresh(force=True)
        out: dict[str, Any] = {}
        for name, tool in sorted(self._tools.items()):
            if name in self._refused:
                out[name] = {"refused": self._refused[name]}
            else:
                out[name] = {
                    "description": tool.get("description") or tool.get("title") or "",
                    "params": tool.get("inputSchema") or {"type": "object"},
                }
        return out

    async def execute(
        self, op: str, params: dict[str, Any], idempotency_key: str | None = None
    ) -> Any:
        try:
            # With pins, every call re-lists first: a server announces a change
            # only when it next hears from us, which would be this very call.
            await self._refresh(force=self.pin)
        except TimeoutError:
            raise ToolError(f"{self.name}: no answer within {self.timeout}s") from None
        if op in self._refused:
            raise ToolError(f"{self.name}.{op} refused: {self._refused[op]}")
        if op not in self._tools:
            raise ToolError(f"{self.name} has no tool {op!r} "
                            f"(tools: {', '.join(self.ops()) or 'none'})")
        await self._respect_rate_limit()
        try:
            result = await self._rpc("tools/call", {"name": op, "arguments": params})
        except TimeoutError:
            raise ToolError(f"{self.name}.{op} timed out after {self.timeout}s") from None
        except Transient as exc:
            # The call may have run: the server died with it in hand.
            raise ToolError(f"{self.name}.{op}: {exc}") from exc
        text = "\n".join(c.get("text", "") for c in result.get("content") or []
                         if isinstance(c, dict) and c.get("type") == "text")
        if result.get("isError"):
            raise ToolError(f"{self.name}.{op}: {text or 'the tool reported an error'}")
        if result.get("structuredContent") is not None:
            return result["structuredContent"]
        others = [c for c in result.get("content") or []
                  if isinstance(c, dict) and c.get("type") != "text"]
        if others:
            return {"text": text, "content": others}
        return text


def shadowing(catalog: dict[str, dict[str, Any]]) -> dict[tuple[str, str], str]:
    """Across the MCP servers one agent holds: a description that names another
    server's tool is trying to steer calls it does not own."""
    owners: dict[str, set[str]] = {}
    for cap, tools in catalog.items():
        for name in tools:
            owners.setdefault(name, set()).add(cap)
    # A one-word name ("search", "read") turns up in honest prose; a compound
    # one ("send_email", "createIssue") is a reference.
    named = {n: caps for n, caps in owners.items()
             if re.search(r"[_\-]|[a-z][A-Z]", n)}
    found: dict[tuple[str, str], str] = {}
    for cap, tools in catalog.items():
        for name, tool in tools.items():
            text = "\n".join(_texts({"d": tool.get("description"), "p": tool.get("params")}))
            for other, caps in named.items():
                foreign = caps - {cap}
                if (foreign and cap not in caps
                        and re.search(rf"(?<![\w.-]){re.escape(other)}(?![\w-])", text)):
                    found[(cap, name)] = (
                        f"its description names {other!r}, a tool of "
                        f"{', '.join(sorted(foreign))}"
                    )
                    break
    return found


def _version() -> str:
    from .. import __version__
    return __version__
