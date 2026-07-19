"""Authentication on the control plane.

Almost every test here is a refusal. The routes hand out the ability to spend
money, stop other people's work, and read what their agents produced, so the
interesting question is never "does the right token work" — it is whether
anything gets through without one.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agentos import RuntimeClient  # noqa: E402
from agentos.kernel.store import Store  # noqa: E402
from agentos.runtime.daemon import Daemon, _is_loopback  # noqa: E402

TOKEN = "s3cret-token-value"


def call(url: str, path: str, token: str | None = None,
         query_token: str | None = None, method: str = "GET",
         body: dict | None = None) -> tuple[int, dict]:
    target = url + path + (f"?token={query_token}" if query_token else "")
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(target, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw[:1] in b"{[" else {})
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        return exc.code, (json.loads(raw) if raw[:1] in b"{[" else {})


class AuthenticatedDaemonTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(self.tmp.name)
        self.daemon = Daemon(store=self.store, port=0, tick=0.01,
                             isolation="task", permissions={}, token=TOKEN)
        self.task = asyncio.create_task(self.daemon.start())
        await asyncio.sleep(0.15)
        self.url = self.daemon.url

    async def asyncTearDown(self):
        self.daemon.stop()
        await asyncio.wait_for(self.task, timeout=20)
        self.store.close()

    # -- nothing gets through unauthenticated --------------------------------
    async def test_every_route_refuses_without_a_token(self):
        routes = [
            ("GET", "/state"), ("GET", "/ps"), ("GET", "/health"),
            ("GET", "/logs"), ("GET", "/events"), ("GET", "/agents/1"),
            ("GET", "/task/1"), ("GET", "/"),
        ]
        for method, path in routes:
            code, _ = await asyncio.to_thread(call, self.url, path, None, None, method)
            self.assertEqual(code, 401, f"{method} {path} was not refused")

    async def test_mutating_routes_refuse_without_a_token(self):
        for path, body in [
            ("/task", {"goal": "spend your money", "tools": []}),
            ("/agents", {"spec": {}}),
            ("/agents/1/kill", {}),
            ("/shutdown", {}),
        ]:
            code, _ = await asyncio.to_thread(
                call, self.url, path, None, None, "POST", body)
            self.assertEqual(code, 401, f"POST {path} was not refused")
        # And the daemon is still up, i.e. /shutdown really was refused.
        code, _ = await asyncio.to_thread(call, self.url, "/health", TOKEN)
        self.assertEqual(code, 200)

    async def test_a_wrong_token_is_refused(self):
        for wrong in ("", "nope", TOKEN[:-1], TOKEN + "x", TOKEN.upper()):
            code, _ = await asyncio.to_thread(call, self.url, "/health", wrong)
            self.assertEqual(code, 401, f"token {wrong!r} was accepted")

    async def test_the_denial_says_nothing_about_the_token_sent(self):
        """A reply that distinguishes "close" from "wrong" is a guessing oracle."""
        _, body = await asyncio.to_thread(call, self.url, "/health", TOKEN[:-1])
        self.assertNotIn(TOKEN[:-1], json.dumps(body))
        self.assertNotIn(TOKEN, json.dumps(body))

    async def test_a_denial_advertises_the_scheme(self):
        req = urllib.request.Request(self.url + "/health")

        def send():
            try:
                urllib.request.urlopen(req, timeout=10)
            except urllib.error.HTTPError as exc:
                return exc.headers.get("WWW-Authenticate", "")
            return ""

        self.assertIn("Bearer", await asyncio.to_thread(send))

    # -- the right token works -----------------------------------------------
    async def test_the_header_form_is_accepted(self):
        code, body = await asyncio.to_thread(call, self.url, "/health", TOKEN)
        self.assertEqual(code, 200)
        self.assertIn("runtime", body)

    async def test_the_query_form_is_accepted_for_the_dashboard(self):
        code, _ = await asyncio.to_thread(
            call, self.url, "/", None, TOKEN)
        self.assertEqual(code, 200)

    async def test_the_client_authenticates_itself_from_the_endpoint_file(self):
        """An application on this machine should need no configuration."""
        client = RuntimeClient(dirpath=self.tmp.name)
        self.assertEqual(client.token, TOKEN)
        health = await asyncio.to_thread(client.health)
        self.assertIn("runtime", health)

    async def test_the_client_fails_clearly_with_the_wrong_token(self):
        client = RuntimeClient(url=self.url, token="wrong")
        with self.assertRaises(RuntimeError) as caught:
            await asyncio.to_thread(client.health)
        self.assertIn("401", str(caught.exception))

    async def test_the_token_is_not_written_to_the_kernel_log(self):
        await asyncio.to_thread(call, self.url, "/health", TOKEN)
        logs = json.dumps(self.store.logs(limit=500))
        self.assertNotIn(TOKEN, logs)


class UnauthenticatedDaemonTest(unittest.IsolatedAsyncioTestCase):
    """No token: loopback still works, so nothing existing breaks."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(self.tmp.name)
        self.daemon = Daemon(store=self.store, port=0, tick=0.01,
                             isolation="task", permissions={})
        self.task = asyncio.create_task(self.daemon.start())
        await asyncio.sleep(0.15)

    async def asyncTearDown(self):
        self.daemon.stop()
        await asyncio.wait_for(self.task, timeout=20)
        self.store.close()

    async def test_loopback_without_a_token_still_serves(self):
        code, _ = await asyncio.to_thread(call, self.daemon.url, "/health")
        self.assertEqual(code, 200)

    async def test_no_token_leaks_into_the_endpoint_file(self):
        data = json.loads(
            (Path(self.tmp.name) / "daemon.json").read_text(encoding="utf-8"))
        self.assertNotIn("token", data)


class ExposureGuardTest(unittest.TestCase):
    """Binding the world with no token is a typo, not a deployment."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(os.environ.pop, "AGENTOS_TOKEN", None)
        os.environ.pop("AGENTOS_TOKEN", None)

    def daemon(self, **kw):
        store = Store(self.tmp.name)
        self.addCleanup(store.close)
        return Daemon(store=store, port=0, tick=0.01, isolation="task", **kw)

    def test_serving_all_interfaces_without_a_token_is_refused(self):
        for host in ("0.0.0.0", "", "192.168.1.50", "::"):
            with self.assertRaises(ValueError, msg=f"{host} was allowed"):
                self.daemon(host=host)

    def test_a_token_makes_it_allowed(self):
        d = self.daemon(host="0.0.0.0", token=TOKEN)
        self.assertEqual(d.token, TOKEN)

    def test_insecure_is_the_explicit_escape_hatch(self):
        d = self.daemon(host="0.0.0.0", insecure=True)
        self.assertIsNone(d.token)

    def test_the_environment_supplies_a_token_too(self):
        os.environ["AGENTOS_TOKEN"] = "from-the-env"
        self.assertEqual(self.daemon(host="0.0.0.0").token, "from-the-env")

    def test_loopback_forms_are_recognised(self):
        for host in ("127.0.0.1", "::1", "localhost", "127.5.5.5"):
            self.assertTrue(_is_loopback(host), host)
        for host in ("0.0.0.0", "", "10.0.0.1", "example.com"):
            self.assertFalse(_is_loopback(host), host)


if __name__ == "__main__":
    unittest.main(verbosity=2)
