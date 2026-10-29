"""A model gateway that makes any provider honour idempotency keys.

    agent gateway --upstream https://api.groq.com/openai/v1     # port 8787
    # then give the client http://127.0.0.1:8787 as its base URL

Model providers bill a request once it arrives, finish it whether or not the
client is still there, and ignore Idempotency-Key. So when an agent crashes
inside a model call, its resume pays for that call again, however durable the
framework. Deduplication has to happen at a receiver, and the provider will
not do it. This is a receiver you run yourself, outside the agent's process.

A request without an Idempotency-Key passes through untouched. A keyed one
follows four rules, each one a failure the crash study observed:

1. The key names the call, not the body. A resumed agent often rebuilds a
   slightly different prompt for the same call. It gets the recorded reply,
   flagged `Idempotency-Drift: true` (or a 409 with --on-drift reject).
2. The gateway owns the upstream call. A client that disconnects does not
   cancel it: the provider bills it anyway, so its reply is kept for the resume.
3. A repeat that arrives while the call is in flight waits for that call.
4. Its own failures are not hidden. A key whose call was in flight when the
   gateway died, or that ended in a 5xx or a broken connection, may have been
   billed. It is sent again, with `Idempotency-Outcome: unknown`.

A reply that no upstream call was made for carries `Idempotent-Replayed: true`.
Only replies the provider may have billed hold a key: a 2xx is kept, a 4xx
(nothing billed) frees the key at once. Keys are scoped to the caller's
credential and the path, and kept for --ttl hours. Stdlib only.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import sqlite3
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS calls (
    scope TEXT NOT NULL, key TEXT NOT NULL, body_hash TEXT NOT NULL,
    state TEXT NOT NULL,  -- pending | done | unknown
    status INTEGER, headers TEXT, body BLOB,
    created REAL NOT NULL, finished REAL, attempts INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (scope, key)
);
"""
# Not sent upstream: the gateway frames its own connection, and asks for an
# uncompressed reply so that what it stores is what it replays.
DROP_REQUEST = {"host", "content-length", "connection", "keep-alive", "accept-encoding",
                "transfer-encoding", "te", "upgrade", "proxy-connection"}
DROP_RESPONSE = {"content-length", "connection", "keep-alive", "transfer-encoding",
                 "content-encoding"}
DEFAULT_PORT = 8787


class Flight:
    """One upstream call, and every request waiting on it."""

    def __init__(self, body_hash: str | None) -> None:
        self.body_hash = body_hash
        self.cond = threading.Condition()
        self.status: int | None = None
        self.headers: list[tuple[str, str]] = []
        self.chunks: list[bytes] = []
        self.done = False
        self.failed: str | None = None

    def head(self, status: int, headers: list[tuple[str, str]]) -> None:
        with self.cond:
            self.status, self.headers = status, headers
            self.cond.notify_all()

    def add(self, chunk: bytes) -> None:
        with self.cond:
            self.chunks.append(chunk)
            self.cond.notify_all()

    def finish(self, failed: str | None) -> None:
        with self.cond:
            self.done, self.failed = True, failed
            self.cond.notify_all()

    def wait(self, ready, timeout: float) -> bool:
        with self.cond:
            return self.cond.wait_for(ready, timeout)

    def after(self, i: int, timeout: float) -> tuple[list[bytes], bool]:
        """Chunks from index i on, once there are any or the call is over."""
        with self.cond:
            self.cond.wait_for(lambda: len(self.chunks) > i or self.done, timeout)
            return self.chunks[i:], self.done


def _streamed(headers: list[tuple[str, str]]) -> bool:
    return any(k.lower() == "content-type" and "text/event-stream" in v
               for k, v in headers)


class Gateway:
    def __init__(
        self,
        upstream: str,
        db: str | Path,
        *,
        ttl_hours: float = 24.0,
        on_drift: str = "replay",
        timeout: float = 600.0,
        log: str | Path | None = None,
    ) -> None:
        if on_drift not in ("replay", "reject"):
            raise ValueError("on_drift must be 'replay' or 'reject'")
        u = urllib.parse.urlsplit(upstream)
        if u.scheme not in ("http", "https") or not u.netloc:
            raise ValueError(f"upstream must be an http(s) URL, got {upstream!r}")
        self.upstream = upstream
        self.scheme, self.netloc, self.prefix = u.scheme, u.netloc, u.path.rstrip("/")
        self.ttl = ttl_hours * 3600
        self.on_drift = on_drift
        self.timeout = timeout
        self.lock = threading.Lock()
        self.flights: dict[tuple[str, str], Flight] = {}
        self.stats = {k: 0 for k in ("passthrough", "upstream", "joined", "replayed",
                                     "drift", "unknown", "rejected", "freed")}
        self.db = sqlite3.connect(str(db), check_same_thread=False, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        # A pending row must be on disk before the call leaves: it is how a
        # restarted gateway knows a call may have been billed.
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript(SCHEMA)
        self.db.execute("UPDATE calls SET state = 'unknown' WHERE state = 'pending'")
        self.db.execute("DELETE FROM calls WHERE state = 'done' AND created < ?",
                        (time.time() - self.ttl,))
        self._log = open(log, "a", encoding="utf-8") if log else None

    def close(self) -> None:
        self.db.close()
        if self._log:
            self._log.close()

    def count(self, what: str) -> None:
        with self.lock:
            self.stats[what] += 1

    def note(self, **record: Any) -> None:
        if self._log is None:
            return
        with self.lock:
            self._log.write(json.dumps({"t": time.time(), **record}) + "\n")
            self._log.flush()

    def admit(self, scope: str, key: str, body_hash: str) -> tuple[str, Any, dict[str, str]]:
        """What a keyed request gets: ("join", flight), ("replay", row) or
        ("start", flight), with the flags its reply will carry."""
        with self.lock:
            flight = self.flights.get((scope, key))
            if flight is not None:
                flags = {"Idempotent-Replayed": "true"}
                if flight.body_hash != body_hash:
                    flags["Idempotency-Drift"] = "true"
                return "join", flight, flags
            row = self.db.execute(
                "SELECT state, body_hash, status, headers, body, created FROM calls "
                "WHERE scope = ? AND key = ?", (scope, key)).fetchone()
            if row is not None and row[0] == "done" and row[5] >= time.time() - self.ttl:
                flags = {"Idempotent-Replayed": "true"}
                if row[1] != body_hash:
                    flags["Idempotency-Drift"] = "true"
                return "replay", row, flags
            flags = {}
            if row is not None and row[0] in ("pending", "unknown"):
                flags["Idempotency-Outcome"] = "unknown"
            flight = Flight(body_hash)
            self.flights[(scope, key)] = flight
            self.db.execute(
                "INSERT INTO calls (scope, key, body_hash, state, created) "
                "VALUES (?, ?, ?, 'pending', ?) ON CONFLICT (scope, key) DO UPDATE SET "
                "state = 'pending', body_hash = excluded.body_hash, status = NULL, "
                "headers = NULL, body = NULL, created = excluded.created, "
                "finished = NULL, attempts = calls.attempts + 1",
                (scope, key, body_hash, time.time()))
            return "start", flight, flags

    def forward(self, flight: Flight, method: str, path: str, body: bytes,
                headers: dict[str, str], scope: str | None = None,
                key: str | None = None) -> None:
        """Make the upstream call. Runs on its own thread, so a client that
        goes away cannot stop it."""
        failed = None
        try:
            cls = (http.client.HTTPSConnection if self.scheme == "https"
                   else http.client.HTTPConnection)
            conn = cls(self.netloc, timeout=self.timeout)
            try:
                conn.request(method, self.prefix + path, body=body or None, headers=headers)
                resp = conn.getresponse()
                flight.head(resp.status, [(k, v) for k, v in resp.getheaders()
                                          if k.lower() not in DROP_RESPONSE])
                while chunk := resp.read1(65536):
                    flight.add(chunk)
            finally:
                conn.close()
        except Exception as exc:
            failed = f"{type(exc).__name__}: {exc}"
        if key is not None:
            self._settle(scope, key, flight, failed)
        flight.finish(failed)

    def _settle(self, scope: str, key: str, flight: Flight, failed: str | None) -> None:
        status = flight.status
        with self.lock:
            if failed is None and status is not None and 200 <= status < 300:
                self.db.execute(
                    "UPDATE calls SET state = 'done', status = ?, headers = ?, body = ?, "
                    "finished = ? WHERE scope = ? AND key = ?",
                    (status, json.dumps(flight.headers), b"".join(flight.chunks),
                     time.time(), scope, key))
            elif failed is None and status is not None and status < 500:
                # Refused (a 429, a bad request): nothing was billed.
                self.db.execute("DELETE FROM calls WHERE scope = ? AND key = ?", (scope, key))
                self.stats["freed"] += 1  # under the lock already
            else:
                self.db.execute(
                    "UPDATE calls SET state = 'unknown', finished = ? "
                    "WHERE scope = ? AND key = ?", (time.time(), scope, key))
            self.flights.pop((scope, key), None)


class Handler(BaseHTTPRequestHandler):
    server_version = "agentos-gateway"

    def log_message(self, *args):  # quiet: the gateway keeps its own log
        pass

    def do_GET(self):
        if self.path == "/_gateway":
            gw = self.server.gateway
            return self._send(200, [("Content-Type", "application/json")], json.dumps(
                {"upstream": gw.upstream, "in_flight": len(gw.flights), **gw.stats}).encode())
        self._handle()

    def do_POST(self):
        self._handle()

    def _handle(self):
        gw: Gateway = self.server.gateway
        started = time.time()
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        fwd = {k: v for k, v in self.headers.items() if k.lower() not in DROP_REQUEST}
        fwd["Accept-Encoding"] = "identity"
        key = self.headers.get("Idempotency-Key")
        if not key or self.command != "POST":
            flight, flags, what = Flight(None), {}, "passthrough"
            gw.count("passthrough")
            threading.Thread(target=gw.forward, daemon=True,
                             args=(flight, self.command, self.path, body, fwd)).start()
        else:
            cred = self.headers.get("Authorization") or self.headers.get("x-api-key") or ""
            scope = hashlib.sha256(f"{cred}\n{self.path}".encode()).hexdigest()[:32]
            what, found, flags = gw.admit(scope, key, hashlib.sha256(body).hexdigest())
            if "Idempotency-Drift" in flags:
                gw.count("drift")
            if "Idempotency-Outcome" in flags:
                gw.count("unknown")
            gw.note(key=key, decision=what, drift="Idempotency-Drift" in flags,
                    unknown="Idempotency-Outcome" in flags)
            if "Idempotency-Drift" in flags and gw.on_drift == "reject" and what != "start":
                gw.count("rejected")
                return self._error(409, "Idempotency-Key reused with a different request", flags)
            if what == "replay":
                gw.count("replayed")
                status, headers = found[2], [tuple(h) for h in json.loads(found[3])]
                headers = [(k, v) for k, v in headers if not k.lower().startswith("x-ratelimit")]
                return self._send(status, headers + list(flags.items()), found[4],
                                  stream=_streamed(headers))
            flight = found
            if what == "join":
                gw.count("joined")
            else:
                gw.count("upstream")
                threading.Thread(target=gw.forward, daemon=True,
                                 args=(flight, "POST", self.path, body, fwd, scope, key)).start()
        self._relay(flight, flags)
        if what == "passthrough":
            return
        gw.note(key=key, finished=what, status=flight.status,
                ms=round((time.time() - started) * 1000, 1))

    def _relay(self, flight: Flight, flags: dict[str, str]) -> None:
        gw: Gateway = self.server.gateway
        if not flight.wait(lambda: flight.status is not None or flight.done, gw.timeout):
            return self._error(504, "upstream did not answer in time", flags)
        if flight.status is None:
            return self._error(502, f"upstream failed: {flight.failed}", flags)
        headers = flight.headers + list(flags.items())
        if not _streamed(flight.headers):
            flight.wait(lambda: flight.done, gw.timeout)
            if flight.failed:
                return self._error(502, f"upstream broke off: {flight.failed}", flags)
            return self._send(flight.status, headers, b"".join(flight.chunks))
        # A stream is relayed as it arrives; the connection's close ends it.
        try:
            self.send_response(flight.status)
            for k, v in headers:
                self.send_header(k, v)
            self.end_headers()
            i = 0
            while True:
                new, done = flight.after(i, gw.timeout)
                for chunk in new:
                    self.wfile.write(chunk)
                self.wfile.flush()
                i += len(new)
                if done and i >= len(flight.chunks):
                    break
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass  # the client died; the call goes on without it

    def _send(self, status: int, headers: list[tuple[str, str]], body: bytes,
              stream: bool = False) -> None:
        try:
            self.send_response(status)
            for k, v in headers:
                self.send_header(k, v)
            if not stream:
                self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def _error(self, status: int, message: str, flags: dict[str, str]) -> None:
        body = json.dumps({"error": {"message": message, "type": "gateway_error"}}).encode()
        self._send(status, [("Content-Type", "application/json"), *flags.items()], body)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 128

    def __init__(self, gateway: Gateway, host: str = "127.0.0.1", port: int = DEFAULT_PORT):
        super().__init__((host, port), Handler)
        self.gateway = gateway


def serve(upstream: str, db: str | Path, host: str = "127.0.0.1", port: int = DEFAULT_PORT,
          **kw: Any) -> int:
    gw = Gateway(upstream, db, **kw)
    srv = Server(gw, host, port)
    # The port first, alone on its line, so a parent that asked for port 0 can read it.
    print(srv.server_address[1], flush=True)
    print(f"agentos gateway at http://{host}:{srv.server_address[1]} -> {upstream}",
          file=sys.stderr, flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
        gw.close()
    return 0
