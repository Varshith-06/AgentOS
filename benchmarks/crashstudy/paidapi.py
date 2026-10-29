"""A paid API in its own process: it bills on receipt and answers after a delay.

    python -m benchmarks.crashstudy.paidapi --ledger L.db --latency 0.3 --port 0

That is the shape of a model API seen from a client that dies: the provider has
done (and charged for) the work whether or not anyone is left to read the reply.
An `Idempotency-Key` header makes a repeat free: the stored reply comes back and
nothing is charged. With --ignore-keys it behaves like the model providers we
measured instead: every request is charged, key or not.

The ledger is the only source of truth the study reads. No framework writes it.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SCHEMA = """
CREATE TABLE IF NOT EXISTS charges (
    id INTEGER PRIMARY KEY AUTOINCREMENT, run TEXT NOT NULL, i INTEGER NOT NULL,
    key TEXT, ts REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS keys (key TEXT PRIMARY KEY, reply TEXT NOT NULL);
"""


def init(ledger: str) -> None:
    conn = sqlite3.connect(ledger)
    conn.executescript(SCHEMA)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.commit()
    conn.close()


def charges(ledger: str, run: str) -> list[int]:
    """The call indices charged for `run`, one entry per charge, in order."""
    conn = sqlite3.connect(ledger, timeout=10)
    try:
        return [r[0] for r in conn.execute(
            "SELECT i FROM charges WHERE run = ? ORDER BY id", (run,))]
    finally:
        conn.close()


class Handler(BaseHTTPRequestHandler):
    ledger: str
    latency: float
    ignore_keys = False
    lock = threading.Lock()

    def log_message(self, *args):  # quiet
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        key = self.headers.get("Idempotency-Key")
        conn = sqlite3.connect(self.ledger, isolation_level=None, timeout=10)
        try:
            with self.lock:
                if key and not self.ignore_keys:
                    row = conn.execute(
                        "SELECT reply FROM keys WHERE key = ?", (key,)).fetchone()
                    if row is not None:
                        return self._send(json.loads(row[0]) | {"replayed": True})
                cur = conn.execute(
                    "INSERT INTO charges (run, i, key, ts) VALUES (?, ?, ?, ?)",
                    (body["run"], body["i"], key, time.time()))
                reply = {"i": body["i"], "charge": cur.lastrowid}
                if key and not self.ignore_keys:
                    conn.execute("INSERT INTO keys VALUES (?, ?)",
                                 (key, json.dumps(reply)))
        finally:
            conn.close()
        time.sleep(float(body.get("latency", self.latency)))
        self._send(reply)

    def _send(self, obj):
        data = json.dumps(obj).encode()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass  # the client died waiting; the charge stands


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ledger", required=True)
    ap.add_argument("--latency", type=float, default=0.3)
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--ignore-keys", action="store_true",
                    help="charge every request, as the model providers we measured do")
    args = ap.parse_args()
    init(args.ledger)
    Handler.ledger, Handler.latency = args.ledger, args.latency
    Handler.ignore_keys = args.ignore_keys
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    srv.daemon_threads = True
    print(srv.server_address[1], flush=True)
    srv.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
