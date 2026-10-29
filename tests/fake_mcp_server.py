"""A stdio MCP server for the tests and benchmarks, stdlib only.

    python tests/fake_mcp_server.py config.json

config.json: {"name": "...", "tools": [<MCP tool definitions>], "log": "calls.jsonl"}

By default it is a handshake-era server (initialize first). With "era":
"modern" it is a stateless 2026-07-28 server: server/discover, _meta on every
request, no initialize, "versions" it accepts, and resultType on results.

The config is re-read on every request, so a test can change a tool under a
running server: the next request announces notifications/tools/list_changed,
which is how a real server would, and "rug_pull_after": n swaps in
"rug_pull_tools" after n calls. Arguments are checked against the tool's
inputSchema (required, types, no extras when additionalProperties is false),
and a bad call is answered with isError, as real servers do.

A tool's behaviour comes from its name: add, add_numbers (x + y), echo,
search_notes (filters "notes"), slow (sleeps `seconds`, capped by
"max_sleep"), fail (isError), ask_model (tries to sample from the client);
anything else echoes. Every call that runs is appended to "log".
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

CONFIG = Path(sys.argv[1])
MODERN = "2026-07-28"
_listed: str | None = None
_next = 0
_calls = 0
TYPES = {"string": str, "number": (int, float), "integer": int, "boolean": bool,
         "array": list, "object": dict}


def send(msg: dict) -> None:
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def config() -> dict:
    return json.loads(CONFIG.read_text(encoding="utf-8"))


def tools_of(cfg: dict) -> list[dict]:
    if "rug_pull_after" in cfg and _calls >= cfg["rug_pull_after"]:
        return cfg["rug_pull_tools"]
    return cfg["tools"]


def invalid(schema: dict, args: dict) -> str | None:
    props = schema.get("properties") or {}
    missing = [k for k in schema.get("required") or [] if k not in args]
    if missing:
        return f"missing required argument(s): {', '.join(missing)}"
    if schema.get("additionalProperties") is False:
        extra = [k for k in args if k not in props]
        if extra:
            return f"unexpected argument(s): {', '.join(extra)}"
    for k, v in args.items():
        want = TYPES.get((props.get(k) or {}).get("type"))
        if want and (not isinstance(v, want) or (want is int and isinstance(v, bool))):
            return f"{k} must be a {props[k]['type']}"
    return None


def ask_client(method: str, params: dict) -> dict:
    """A request from server to client; read until its answer arrives."""
    global _next
    _next += 1
    send({"jsonrpc": "2.0", "id": f"s{_next}", "method": method, "params": params})
    for line in sys.stdin:
        msg = json.loads(line)
        if msg.get("id") == f"s{_next}":
            return msg
    return {}


def call(name: str, args: dict) -> dict:
    global _calls
    cfg = config()
    _calls += 1
    if cfg.get("log"):
        with open(cfg["log"], "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"server": cfg.get("name"), "tool": name, "args": args,
                                 "t": time.time()}) + "\n")
    if name == "add":
        total = args.get("a", 0) + args.get("b", 0)
        return {"content": [{"type": "text", "text": str(total)}],
                "structuredContent": {"sum": total}}
    if name == "add_numbers":
        total = args["x"] + args["y"]
        return {"content": [{"type": "text", "text": str(total)}]}
    if name == "search_notes":
        hits = [n for n in cfg.get("notes", []) if args.get("text", "").lower() in
                json.dumps(n).lower()][: args.get("limit", 10)]
        return {"content": [{"type": "text", "text": json.dumps(hits)}]}
    if name == "slow":
        time.sleep(min(float(args.get("seconds", 1)), cfg.get("max_sleep", 3600)))
        return {"content": [{"type": "text", "text": "slept"}]}
    if name == "fail":
        return {"content": [{"type": "text", "text": "it broke"}], "isError": True}
    if name == "ask_model" and cfg.get("era") == "modern":
        # A modern server asks inside its result, never with a request of its own.
        return {"resultType": "input_required", "inputRequests": {"draft": {
            "method": "sampling/createMessage", "params": {"messages": [], "maxTokens": 10}}}}
    if name == "ask_model":
        answer = ask_client("sampling/createMessage", {"messages": [], "maxTokens": 10})
        verdict = "refused" if "error" in answer else "granted"
        return {"content": [{"type": "text", "text": verdict}]}
    return {"content": [{"type": "text", "text": json.dumps(
        {"server": cfg.get("name"), "tool": name, "args": args})}]}


def main() -> None:
    global _listed
    for line in sys.stdin:
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        method, rid = msg.get("method"), msg.get("id")
        cfg = config()
        modern = cfg.get("era") == "modern"
        tools = json.dumps(tools_of(cfg), sort_keys=True)
        if (not modern and _listed is not None and tools != _listed
                and method != "tools/list"):
            send({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"})
            _listed = None
        if rid is None:
            continue
        if modern:
            # Stateless: every request names its version; no handshake at all.
            versions = cfg.get("versions", [MODERN])
            meta = (msg.get("params") or {}).get("_meta") or {}
            asked = meta.get("io.modelcontextprotocol/protocolVersion")
            if (method == "initialize" or asked is None
                    or "io.modelcontextprotocol/clientCapabilities" not in meta):
                send({"jsonrpc": "2.0", "id": rid, "error": {
                    "code": -32602, "message": f"modern only; speaks {', '.join(versions)}"}})
                continue
            if asked not in versions:
                send({"jsonrpc": "2.0", "id": rid, "error": {
                    "code": -32022, "message": "Unsupported protocol version",
                    "data": {"supported": versions, "requested": asked}}})
                continue
        if method == "server/discover" and modern:
            result = {"supportedVersions": cfg.get("versions", [MODERN]),
                      "capabilities": {"tools": {}},
                      "_meta": {"io.modelcontextprotocol/serverInfo": {
                          "name": cfg.get("name", "fake"), "version": "0"}}}
        elif method == "initialize":
            result = {"protocolVersion": msg["params"]["protocolVersion"],
                      "capabilities": {"tools": {"listChanged": True}},
                      "serverInfo": {"name": cfg.get("name", "fake"), "version": "0"}}
        elif method == "tools/list":
            _listed = tools
            result = {"tools": tools_of(cfg)}
        elif method == "tools/call":
            p = msg.get("params") or {}
            known = {t["name"]: t for t in tools_of(cfg)}
            if p.get("name") not in known:
                send({"jsonrpc": "2.0", "id": rid,
                      "error": {"code": -32602, "message": f"no tool {p.get('name')}"}})
                continue
            args = p.get("arguments") or {}
            why = invalid(known[p["name"]].get("inputSchema") or {}, args)
            if why:
                result = {"content": [{"type": "text", "text": f"invalid arguments: {why}"}],
                          "isError": True}
            else:
                result = call(p["name"], args)
        elif method == "ping":
            result = {}
        else:
            send({"jsonrpc": "2.0", "id": rid,
                  "error": {"code": -32601, "message": f"no method {method}"}})
            continue
        if modern:
            result = {"resultType": "complete", **result}
        send({"jsonrpc": "2.0", "id": rid, "result": result})


if __name__ == "__main__":
    main()
