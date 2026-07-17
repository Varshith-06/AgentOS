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
    GET  /logs?limit=  /events?limit=
    POST /agents                      {"spec": ...} -> {"pid": ...}
    POST /agents/<pid>/kill|pause|resume
    POST /agents/<pid>/approve        {"role": ...}
    POST /shutdown
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .. import __version__
from ..kernel.store import Store
from .dashboard import DASHBOARD_HTML


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

        def _limit(self, query: dict) -> int:
            return int(query.get("limit", ["200"])[0])

        # -- reads: straight from the store --------------------------------
        def do_GET(self) -> None:
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
                        "runtime": store.runtime_info(),
                    })
                elif parts == ["ps"]:
                    self._json(200, {
                        "runtime": store.runtime_info(),
                        "processes": store.processes(),
                        "costs": store.model_costs(),
                        "memory": store.memory_usage(),
                    })
                elif len(parts) == 2 and parts[0] == "agents":
                    rows = [r for r in store.processes() if r["pid"] == int(parts[1])]
                    if rows:
                        self._json(200, rows[0])
                    else:
                        self._json(404, {"error": f"no such agent: pid {parts[1]}"})
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
            parts = [p for p in urlparse(self.path).path.split("/") if p]
            try:
                body = self._body()
                kernel = daemon.kernel
                if parts == ["agents"]:
                    pid = daemon.call(lambda: kernel.submit_spec(body["spec"]))
                    self._json(200, {"pid": pid})
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
            except Exception as exc:
                self._json(400, {"error": f"{type(exc).__name__}: {exc}"})

    return ThreadingHTTPServer((host, port), Handler)
