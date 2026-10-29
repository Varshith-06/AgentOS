"""The gateway: a provider that ignores Idempotency-Key, made to honour it."""

from __future__ import annotations

import asyncio
import json
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agentos import Agent, Kernel  # noqa: E402
from agentos.api.gateway import Gateway, Server  # noqa: E402
from agentos.kernel.memory import MemoryManager  # noqa: E402
from agentos.kernel.store import Store  # noqa: E402

from tests._timing import LIMIT  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


class Provider(BaseHTTPRequestHandler):
    """Like a model provider: bills on receipt, finishes even if the client is
    gone, ignores Idempotency-Key. The body says how to answer."""

    received: list[dict]
    lock = threading.Lock()
    script: list[int]  # statuses to answer with, in order, then 200s

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])) or b"{}")
        with self.lock:
            self.received.append({"path": self.path, "body": body,
                                  "auth": self.headers.get("Authorization")})
            n = len(self.received)
            status = self.script.pop(0) if self.script else 200
        time.sleep(body.get("latency", 0))
        try:
            if body.get("stream"):
                self.send_response(status)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for i in range(body["stream"]):
                    self.wfile.write(f"data: {{\"n\": {n}, \"chunk\": {i}}}\n\n".encode())
                    self.wfile.flush()
                    time.sleep(body.get("gap", 0))
                self.wfile.write(b"data: [DONE]\n\n")
                return
            reply = json.dumps({
                "n": n, "id": f"gen-{n}",
                "choices": [{"message": {"content": f"reply {n}"}}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2},
            }).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(reply)))
            self.send_header("x-ratelimit-remaining-requests", str(1000 - n))
            self.end_headers()
            self.wfile.write(reply)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass


def _serve(srv) -> None:
    threading.Thread(target=srv.serve_forever, daemon=True).start()


class GatewayTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        handler = type("P", (Provider,), {"received": [], "script": []})
        self.provider = handler
        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.upstream.daemon_threads = True
        _serve(self.upstream)
        self.addCleanup(self.upstream.shutdown)
        self.base = f"http://127.0.0.1:{self.upstream.server_address[1]}/v1"
        self.gw = self.start_gateway()

    def start_gateway(self, **kw) -> str:
        gw = Gateway(self.base, self.dir / "gw.db", **kw)
        srv = Server(gw, port=0)
        _serve(srv)
        self.addCleanup(gw.close)
        self.addCleanup(srv.shutdown)
        self.gateway = gw
        return f"http://127.0.0.1:{srv.server_address[1]}"

    def calls(self) -> int:
        return len(self.provider.received)

    def post(self, body: dict, key: str | None = None, auth: str = "Bearer a",
             url: str | None = None) -> tuple[int, dict, bytes]:
        headers = {"Content-Type": "application/json", "Authorization": auth}
        if key:
            headers["Idempotency-Key"] = key
        req = urllib.request.Request(f"{url or self.gw}/chat/completions",
                                     data=json.dumps(body).encode(), headers=headers,
                                     method="POST")
        try:
            with urllib.request.urlopen(req, timeout=LIMIT) as resp:
                return resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, {k.lower(): v for k, v in e.headers.items()}, e.read()

    def until(self, predicate, timeout=LIMIT):
        end = time.time() + timeout
        while not predicate():
            if time.time() > end:
                self.fail("timed out")
            time.sleep(0.01)

    def test_a_request_without_a_key_passes_through_every_time(self):
        self.post({"q": 1})
        self.post({"q": 1})
        self.assertEqual(self.calls(), 2)
        self.assertEqual(self.provider.received[0]["path"], "/v1/chat/completions")

    def test_a_finished_call_is_answered_from_the_record(self):
        s1, h1, b1 = self.post({"q": 1}, key="k1")
        s2, h2, b2 = self.post({"q": 1}, key="k1")
        self.assertEqual((s1, s2), (200, 200))
        self.assertEqual(b1, b2)
        self.assertEqual(self.calls(), 1, "the provider saw it once")
        self.assertNotIn("idempotent-replayed", h1)
        self.assertEqual(h2["idempotent-replayed"], "true")
        self.assertNotIn("x-ratelimit-remaining-requests", h2, "a replay's quota is stale")

    def test_a_repeat_while_the_call_is_in_flight_waits_for_it(self):
        first = {}
        t = threading.Thread(target=lambda: first.update(r=self.post({"latency": 0.5}, key="k")))
        t.start()
        self.until(lambda: self.calls() == 1)
        s, h, b = self.post({"latency": 0.5}, key="k")
        t.join(LIMIT)
        self.assertEqual(self.calls(), 1)
        self.assertEqual(b, first["r"][2])
        self.assertEqual(h["idempotent-replayed"], "true")

    def test_many_repeats_at_once_make_one_call(self):
        out = []
        threads = [threading.Thread(target=lambda: out.append(self.post({"latency": 0.2}, key="k")))
                   for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(LIMIT)
        self.assertEqual(self.calls(), 1)
        self.assertEqual(len({b for _, _, b in out}), 1)

    def test_a_client_that_dies_mid_call_does_not_cancel_it(self):
        port = int(self.gw.rsplit(":", 1)[1])
        body = json.dumps({"latency": 0.4}).encode()
        with socket.create_connection(("127.0.0.1", port)) as s:
            s.sendall(b"POST /chat/completions HTTP/1.1\r\nHost: x\r\n"
                      b"Authorization: Bearer a\r\nIdempotency-Key: k\r\n"
                      b"Content-Type: application/json\r\n"
                      + f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
            self.until(lambda: self.calls() == 1)
        # The client is gone. The provider bills the call anyway; the gateway
        # keeps its reply for whoever asks under the same key.
        self.until(lambda: not self.gateway.flights)
        s, h, b = self.post({"latency": 0.4}, key="k")
        self.assertEqual(s, 200)
        self.assertEqual(self.calls(), 1)
        self.assertEqual(json.loads(b)["n"], 1)
        self.assertEqual(h["idempotent-replayed"], "true")

    def test_a_stream_is_relayed_live_and_replayed_whole(self):
        body = {"stream": 3, "gap": 0.05}
        s1, h1, b1 = self.post(body, key="k")
        s2, h2, b2 = self.post(body, key="k")
        self.assertEqual(self.calls(), 1)
        self.assertEqual(b1, b2)
        self.assertIn(b"data: [DONE]", b2)
        self.assertEqual(h2["content-type"], "text/event-stream")

    def test_a_refusal_frees_the_key(self):
        self.provider.script = [429]
        s1, _, _ = self.post({"q": 1}, key="k")
        s2, h2, _ = self.post({"q": 1}, key="k")
        self.assertEqual((s1, s2), (429, 200))
        self.assertEqual(self.calls(), 2, "a 429 was not billed, so it is sent again")
        self.assertNotIn("idempotency-outcome", h2)
        self.assertNotIn("idempotent-replayed", h2)

    def test_a_server_error_is_sent_again_but_marked_unknown(self):
        self.provider.script = [500]
        s1, _, _ = self.post({"q": 1}, key="k")
        s2, h2, _ = self.post({"q": 1}, key="k")
        self.assertEqual((s1, s2), (500, 200))
        self.assertEqual(self.calls(), 2)
        self.assertEqual(h2["idempotency-outcome"], "unknown",
                         "a 5xx may have been billed: the repeat must say so")

    def test_a_changed_body_is_replayed_and_flagged(self):
        _, _, b1 = self.post({"q": "prompt as first built"}, key="k")
        s, h, b2 = self.post({"q": "prompt as rebuilt after a crash"}, key="k")
        self.assertEqual((s, b2), (200, b1))
        self.assertEqual(h["idempotency-drift"], "true")
        self.assertEqual(self.calls(), 1)

    def test_a_changed_body_can_be_refused_instead(self):
        url = self.start_gateway(on_drift="reject")
        self.post({"q": 1}, key="k", url=url)
        s, h, _ = self.post({"q": 2}, key="k", url=url)
        self.assertEqual(s, 409)
        self.assertEqual(self.calls(), 1)

    def test_keys_belong_to_one_credential(self):
        self.post({"q": 1}, key="k", auth="Bearer a")
        _, h, _ = self.post({"q": 1}, key="k", auth="Bearer b")
        self.assertEqual(self.calls(), 2, "another caller's key is not yours")
        self.assertNotIn("idempotent-replayed", h)

    def test_an_expired_key_is_a_new_call(self):
        url = self.start_gateway(ttl_hours=0)
        self.post({"q": 1}, key="k", url=url)
        self.post({"q": 1}, key="k", url=url)
        self.assertEqual(self.calls(), 2)

    def test_replies_outlive_the_gateway(self):
        self.post({"q": 1}, key="k")
        url = self.start_gateway()  # a second gateway on the same database
        _, h, _ = self.post({"q": 1}, key="k", url=url)
        self.assertEqual(self.calls(), 1)
        self.assertEqual(h["idempotent-replayed"], "true")

    def test_a_gateway_killed_mid_call_says_so_after_a_restart(self):
        db = self.dir / "killed.db"

        def gateway():
            p = subprocess.Popen(
                [sys.executable, "-m", "agentos.cli", "--dir", str(self.dir / "rt"),
                 "gateway", "--upstream", self.base, "--port", "0", "--db", str(db)],
                cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            self.addCleanup(p.kill)
            return p, f"http://127.0.0.1:{int(p.stdout.readline())}"

        p, url = gateway()

        def doomed():
            try:
                self.post({"latency": 1.0}, key="k", url=url)
            except OSError:
                pass  # its gateway is killed under it

        t = threading.Thread(target=doomed, daemon=True)
        t.start()
        self.until(lambda: self.calls() == 1)
        p.kill()  # kill -9: the call is billed, its reply has nowhere to go
        p.wait(LIMIT)
        _, url = gateway()
        s, h, _ = self.post({"latency": 0}, key="k", url=url)
        self.assertEqual(s, 200)
        self.assertEqual(self.calls(), 2, "it cannot know the first was billed")
        self.assertEqual(h["idempotency-outcome"], "unknown")


class Asker(Agent):
    async def run(self, ctx):
        out = await ctx.request_model("fast", "say something")
        return out["text"]


class KernelThroughGatewayTest(unittest.IsolatedAsyncioTestCase):
    """AgentOS sends its journalled key on model calls when the endpoint dedups."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.store = Store(self.dir / "rt")
        self.addCleanup(self.store.close)
        handler = type("P", (Provider,), {"received": [], "script": []})
        self.provider = handler
        up = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        up.daemon_threads = True
        _serve(up)
        self.addCleanup(up.shutdown)
        gw = Gateway(f"http://127.0.0.1:{up.server_address[1]}/v1", self.dir / "gw.db")
        srv = Server(gw, port=0)
        _serve(srv)
        self.addCleanup(gw.close)
        self.addCleanup(srv.shutdown)
        self.url = f"http://127.0.0.1:{srv.server_address[1]}"

    def kernel(self, idempotent: bool, latency: float, **kw) -> Kernel:
        model = {"provider": "openai", "base_url": self.url, "api_key_env": None,
                 "model": "m", "params": {"latency": latency}, "idempotent": idempotent}
        return Kernel(store=self.store, tick=0.01, models={"classes": {"fast": [model]}},
                      **kw)

    async def _until(self, predicate):
        async def poll():
            while not predicate():
                await asyncio.sleep(0.01)

        await asyncio.wait_for(poll(), LIMIT)

    async def crash_mid_model_call(self, idempotent: bool):
        k1 = self.kernel(idempotent, latency=1.0)
        pid = k1.spawn(Asker())
        run1 = asyncio.create_task(k1.run())
        await self._until(lambda: len(self.provider.received) == 1)
        # kill -9, in-process: every task dies at once, nothing cleans up.
        scratch_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(scratch_dir.cleanup)
        scratch = Store(scratch_dir.name)
        self.addCleanup(scratch.close)
        k1.store, k1.memory = scratch, MemoryManager(scratch)
        tasks = [run1, *(p.task for p in k1.table.all() if p.task), *k1._io_tasks]
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

        k2 = self.kernel(idempotent, latency=0.0, recover=True)
        await asyncio.wait_for(k2.run(), LIMIT)
        return k2, pid

    async def test_a_model_call_in_flight_is_billed_once(self):
        k2, pid = await self.crash_mid_model_call(idempotent=True)
        self.assertEqual(k2.table.get(pid).result, "reply 1", "the first call's answer")
        self.assertEqual(len(self.provider.received), 1)
        self.assertEqual([e for e in k2.bus.history if e.type == "OutcomeAmbiguous"], [])
        said = [r["message"] for r in self.store.logs(pid=pid)]
        self.assertTrue(any("gateway's record" in m for m in said), said)

    async def test_without_the_gateway_contract_it_is_billed_twice_and_flagged(self):
        k2, pid = await self.crash_mid_model_call(idempotent=False)
        self.assertEqual(len(self.provider.received), 2)
        self.assertEqual(
            len([e for e in k2.bus.history if e.type == "OutcomeAmbiguous"]), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
