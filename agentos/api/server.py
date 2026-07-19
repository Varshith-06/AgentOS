"""The daemon's control plane: HTTP + JSON (Phase 7, p.8).

Deliberately stdlib — like the browser driver and the semantic embedding, the
interface is the point and the implementation is humble. A FastAPI app could
replace this file without any client or kernel change; the routes ARE the API.

Reads are served straight from the store (SQLite is the read model, exactly as
it was for the CLI since Phase 1). Mutations hop onto the kernel's event loop
via Daemon.call, so kernel state is only ever touched from its own thread.

    GET  /                            the dashboard (Phase 8)
    GET  /state                       scheduler snapshot: deps, ready, running
    GET  /health                      runtime info
    GET  /ps                          processes + costs + memory usage
    GET  /agents/<pid>                one row (result included when finished)
    GET  /task/<pid>                  a task's result plus the team it created
    GET  /logs?limit=  /events?limit=
    POST /agents                      {"spec":..., "grant":[...]} -> {"pid":...}
    POST /task                        {"goal":..., "tools":[...]} -> {"pid":...}
    POST /agents/<pid>/kill|pause|resume
    POST /agents/<pid>/approve        {"role": ...}
    POST /shutdown

`grant` is the capability ceiling for everything the submitted agent goes on
to create (see kernel/permissions.py). It is the security-relevant field on
this API: without it a submitted agent falls back to the name matrix, and with
it no descendant can exceed what was granted here.

POST /task is the doorway for work that has no predefined shape — a sentence
and a tool list in, a planner out. What it may grant is bounded twice: by the
daemon's own `task_tools` allowlist, which the operator sets when starting the
runtime, and by the caller's request within it.

Authentication
--------------
Every route requires a bearer token when the daemon has one. There are no
"safe" reads to exempt: `/ps` carries other applications' goals and results,
`/logs` carries whatever agents logged, and `/shutdown` stops the runtime. A
daemon with no token configured is unauthenticated, which is fine on loopback
and refused outright on any other interface — see Daemon.__init__.

    Authorization: Bearer <token>      the normal path
    ?token=<token>                     for the dashboard, which is a browser
                                       and cannot set a header on a page load

The query form is a deliberate trade: it is convenient and it leaks into
Referer headers and shell history in a way the header does not. Prefer the
header for anything programmatic.
"""

from __future__ import annotations

import hmac
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .. import __version__
from ..drivers import REGISTRY
from ..kernel.store import Store
from .dashboard import DASHBOARD_HTML

MAX_GOAL_CHARS = 4000
MAX_STEPS = 50
MAX_CHILDREN = 20


class BadRequest(Exception):
    """The caller sent something this endpoint will not act on."""


def _task_request(daemon, body: dict) -> tuple[dict, list[str]]:
    """Validate a /task body and return (planner spec, grant).

    Everything here is caller-supplied and arrives over a socket, so nothing
    is trusted: the goal is bounded, the tool list must name real drivers, and
    the request can only ask for capabilities the operator already allowed.
    """
    from ..agents.base import spec_of
    from ..agents.llm import LLMAgent

    goal = body.get("goal")
    if not isinstance(goal, str) or not goal.strip():
        raise BadRequest('"goal" must be a non-empty string')
    if len(goal) > MAX_GOAL_CHARS:
        raise BadRequest(f'"goal" is longer than {MAX_GOAL_CHARS} characters')

    tools = body.get("tools", [])
    if not isinstance(tools, list) or any(not isinstance(t, str) for t in tools):
        raise BadRequest('"tools" must be a list of capability names')
    unknown = [t for t in tools if t not in REGISTRY]
    if unknown:
        raise BadRequest(
            f"unknown capabilit{'y' if len(unknown) == 1 else 'ies'}: "
            f"{', '.join(unknown)}. Known: {', '.join(sorted(REGISTRY))}"
        )
    allowed = daemon.task_tools
    refused = [t for t in tools if t not in allowed]
    if refused:
        raise BadRequest(
            f"this runtime does not allow {', '.join(refused)} for submitted "
            f"tasks. It allows: {', '.join(sorted(allowed)) or 'no tools at all'}"
            " (set --task-tools when starting the daemon)"
        )

    def bounded(key: str, default: int, cap: int) -> int:
        value = body.get(key, default)
        if not isinstance(value, int) or value < 1:
            raise BadRequest(f'"{key}" must be a positive integer')
        return min(value, cap)

    retries = body.get("retries", 0)
    if not isinstance(retries, int) or retries < 0:
        raise BadRequest('"retries" must be a non-negative integer')

    # Spending is the one resource a caller can consume without limit, so the
    # operator's ceiling applies the same way the tool allowlist does: the
    # request may ask for less, never more.
    ceiling = daemon.task_budget_usd
    if "budget_usd" not in body:
        budget = ceiling
    else:
        budget = body["budget_usd"]
        if budget is None:
            # Explicit null means "unmetered". Honouring that under a ceiling
            # would let a caller opt out of the cap by asking to, which is
            # not a cap at all.
            if ceiling is not None:
                raise BadRequest(
                    f"this runtime caps submitted tasks at ${ceiling:.4f}; "
                    '"budget_usd": null is not allowed'
                )
        elif not isinstance(budget, (int, float)) or isinstance(budget, bool) \
                or budget <= 0:
            raise BadRequest('"budget_usd" must be a positive number')
        else:
            budget = float(budget)
            if ceiling is not None and budget > ceiling:
                raise BadRequest(
                    f"this runtime caps submitted tasks at ${ceiling:.4f}; "
                    f"${budget:.4f} was requested"
                )

    planner = LLMAgent(
        role=str(body.get("role") or "Planner"),
        goal=goal,
        tools=list(tools),
        model=str(body.get("model") or "fast"),
        child_model=body.get("child_model") or body.get("model") or "fast",
        may_spawn=True,
        max_steps=bounded("max_steps", 12, MAX_STEPS),
        max_children=bounded("max_children", 8, MAX_CHILDREN),
        retries=min(retries, 5),
        context=body.get("context"),
    )
    priority = body.get("priority")
    if priority is not None and priority not in ("High", "Normal", "Low"):
        raise BadRequest('"priority" must be "High", "Normal", or "Low"')
    spec = spec_of(planner)
    if priority is not None:
        spec["priority"] = priority
    return spec, list(tools), budget


def _task_tree(store, pid: int) -> dict | None:
    """A task's root row plus every agent it created, at any depth."""
    rows = {r["pid"]: r for r in store.processes()}
    root = rows.get(pid)
    if root is None:
        return None
    tree, frontier = [], [pid]
    while frontier:
        current = frontier.pop()
        for r in rows.values():
            if r["parent"] == current:
                tree.append(r)
                frontier.append(r["pid"])
    return {
        "pid": pid,
        "status": root["status"],
        "result": root["result"],
        "error": root["exit_reason"] if root["status"] == "Failed" else None,
        "goal": (root.get("spec") or {}).get("params", {}).get("goal"),
        "agents": sorted(tree, key=lambda r: r["pid"]),
    }


def make_server(daemon, host: str, port: int) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args) -> None:
            pass  # the kernel log is the log

        # -- plumbing -----------------------------------------------------
        def _json(self, code: int, payload) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(length)) if length else {}

        # -- authentication -------------------------------------------------
        def _authorized(self) -> bool:
            """Constant-time compare, so a wrong token cannot be guessed a
            character at a time by measuring how long the answer took."""
            expected = daemon.token
            if not expected:
                return True  # unauthenticated daemon; loopback-only by construction
            header = self.headers.get("Authorization", "")
            if header.startswith("Bearer "):
                presented = header[7:].strip()
            else:
                query = parse_qs(urlparse(self.path).query)
                presented = query.get("token", [""])[0]
            return hmac.compare_digest(presented, expected)

        def _deny(self) -> None:
            # The reply says a token is needed and nothing about the one sent:
            # confirming "close but wrong" is help an attacker can use.
            body = json.dumps({"error": "unauthorized: a bearer token is required"})
            raw = body.encode("utf-8")
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Bearer realm="agentos"')
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _limit(self, query: dict) -> int:
            return int(query.get("limit", ["200"])[0])

        # -- reads: straight from the store --------------------------------
        def do_GET(self) -> None:
            if not self._authorized():
                self._deny()
                return
            url = urlparse(self.path)
            parts = [p for p in url.path.split("/") if p]
            query = parse_qs(url.query)
            if not parts or parts == ["dashboard"]:
                body = DASHBOARD_HTML.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if parts == ["state"]:
                try:
                    self._json(200, daemon.call(daemon.kernel.snapshot))
                except Exception as exc:
                    self._json(500, {"error": f"{type(exc).__name__}: {exc}"})
                return
            store = Store(daemon.store.dir)
            try:
                if parts == ["health"]:
                    self._json(200, {
                        "version": __version__,
                        "url": daemon.url,
                        "isolation": daemon.kernel.isolation,
                        "transport": daemon.kernel.transport,
                        "runtime": store.runtime_info(),
                    })
                elif parts == ["ps"]:
                    self._json(200, {
                        "runtime": store.runtime_info(),
                        "processes": store.processes(),
                        "costs": store.model_costs(),
                        "memory": store.memory_usage(),
                        "models": store.model_usage(),
                        "tools": store.tool_usage(),
                    })
                elif len(parts) == 2 and parts[0] == "agents":
                    rows = [r for r in store.processes() if r["pid"] == int(parts[1])]
                    if rows:
                        self._json(200, rows[0])
                    else:
                        self._json(404, {"error": f"no such agent: pid {parts[1]}"})
                elif len(parts) == 2 and parts[0] == "task":
                    tree = _task_tree(store, int(parts[1]))
                    if tree is None:
                        self._json(404, {"error": f"no such task: pid {parts[1]}"})
                    else:
                        self._json(200, tree)
                elif parts == ["logs"]:
                    self._json(200, store.logs(limit=self._limit(query)))
                elif parts == ["events"]:
                    self._json(200, store.events(limit=self._limit(query)))
                else:
                    self._json(404, {"error": f"no such route: GET {url.path}"})
            except Exception as exc:
                self._json(500, {"error": f"{type(exc).__name__}: {exc}"})
            finally:
                store.close()

        # -- mutations: onto the kernel's own thread ------------------------
        def do_POST(self) -> None:
            if not self._authorized():
                self._deny()
                return
            parts = [p for p in urlparse(self.path).path.split("/") if p]
            try:
                body = self._body()
                kernel = daemon.kernel
                if parts == ["agents"]:
                    grant = body.get("grant")
                    if grant is not None and (
                        not isinstance(grant, list)
                        or any(not isinstance(g, str) for g in grant)
                    ):
                        raise BadRequest('"grant" must be a list of capability names')
                    pid = daemon.call(
                        lambda: kernel.submit_spec(body["spec"], grant=grant)
                    )
                    self._json(200, {"pid": pid})
                elif parts == ["task"]:
                    spec, grant, budget = _task_request(daemon, body)
                    pid = daemon.call(lambda: kernel.submit_spec(
                        spec, grant=grant, budget_usd=budget))
                    self._json(200, {
                        "pid": pid,
                        "granted": grant,
                        "budget_usd": budget,
                        "poll": f"/task/{pid}",
                    })
                elif len(parts) == 3 and parts[0] == "agents":
                    pid = int(parts[1])
                    action = parts[2]
                    if action == "kill":
                        daemon.call(lambda: kernel.kill(pid))
                    elif action == "pause":
                        daemon.call(lambda: kernel.pause(pid))
                    elif action == "resume":
                        daemon.call(lambda: kernel.resume(pid))
                    elif action == "approve":
                        daemon.call(lambda: kernel.approve(pid, body["role"]))
                    else:
                        self._json(404, {"error": f"no such action: {action}"})
                        return
                    self._json(200, {"ok": True})
                elif parts == ["shutdown"]:
                    daemon.stop()
                    self._json(200, {"ok": True})
                else:
                    self._json(404, {"error": f"no such route: POST {self.path}"})
            except BadRequest as exc:
                self._json(400, {"error": str(exc)})
            except Exception as exc:
                self._json(400, {"error": f"{type(exc).__name__}: {exc}"})

    return ThreadingHTTPServer((host, port), Handler)
