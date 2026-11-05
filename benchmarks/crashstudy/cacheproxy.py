"""The simple baseline: a response cache in a process of its own, outside the crash.

    python -m benchmarks.crashstudy.cacheproxy --upstream http://127.0.0.1:8000 [--coalesce]

It stores each reply under a hash of the request and answers a repeat from the store,
so it needs no key from the caller, and a changed prompt hashes differently and gets
its own call. It always finishes an upstream call, whether or not the caller is still
there. Without --coalesce, a repeat that arrives while the first request is still out
is sent upstream too, which is what a plain cache does. With --coalesce it waits for
the first. Either way it merges every identical request, including one the caller
meant to make twice.
"""

from __future__ import annotations

import argparse
import hashlib
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

store: dict[str, tuple[int, bytes]] = {}
flying: dict[str, threading.Event] = {}
lock = threading.Lock()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--upstream", required=True)
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--coalesce", action="store_true")
    args = ap.parse_args()

    def forward(path: str, body: bytes, headers) -> tuple[int, bytes]:
        keep = {k: v for k, v in headers.items() if k.lower() in ("content-type", "authorization")}
        req = urllib.request.Request(args.upstream + path, data=body, headers=keep, method="POST")
        with urllib.request.urlopen(req, timeout=600) as resp:
            return resp.status, resp.read()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_a) -> None:
            pass

        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            key = hashlib.sha256(self.path.encode() + b"\0" + body).hexdigest()
            while True:
                with lock:
                    if key in store:
                        status, reply = store[key]
                        break
                    event = flying.get(key) if args.coalesce else None
                    if event is None:
                        mine = threading.Event()
                        if args.coalesce:
                            flying[key] = mine
                        event = None
                if event is not None:
                    event.wait(timeout=600)
                    continue
                try:
                    status, reply = forward(self.path, body, self.headers)
                    with lock:
                        store[key] = (status, reply)
                finally:
                    with lock:
                        flying.pop(key, None)
                    mine.set()
                break
            try:
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(reply)))
                self.end_headers()
                self.wfile.write(reply)
            except OSError:
                pass  # the caller died; the reply stays in the store

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(server.server_address[1], flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
