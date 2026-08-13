"""The shared runtime daemon (Phase 7, p.8)."""

from __future__ import annotations

import asyncio
import concurrent.futures
import ipaddress
import json
import os
import threading
from typing import Any, Callable

from ..api import make_server
from ..kernel.kernel import Kernel
from ..kernel.models import DEFAULT_MODELS_CONFIG
from ..kernel.store import Store


def _is_loopback(host: str) -> bool:
    """Is this address reachable only from this machine?"""
    if not host or host in ("0.0.0.0", "::", "*"):
        return False
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host.lower() in ("localhost", "localhost.localdomain")


class Daemon:
    def __init__(
        self,
        store: Store | None = None,
        dirpath: str = ".agentos",
        host: str = "127.0.0.1",
        port: int = 7070,
        policy: str = "fifo",
        slots: int = 4,
        tick: float = 0.05,
        recover: bool = False,
        models: Any = None,
        permissions: Any = None,
        tools: dict[str, dict[str, Any]] | None = None,
        task_tools: list[str] | None = None,
        task_budget_usd: float | None = None,
        token: str | None = None,
        insecure: bool = False,
    ) -> None:
        self.task_budget_usd = task_budget_usd
        # A daemon with no token is unauthenticated, which is only defensible
        # because nothing off this machine can reach loopback.
        self.token = token or os.environ.get("AGENTOS_TOKEN") or None
        if not self.token and not _is_loopback(host) and not insecure:
            raise ValueError(
                f"refusing to serve {host} without a token: every route would "
                "be open to anyone who can reach the port. Set AGENTOS_TOKEN "
                "(or pass --token), or pass --insecure if something in front "
                "of this already authenticates."
            )
        self.store = store if store is not None else Store(dirpath)
        self.task_tools: set[str] = set(task_tools or ())

        models_path = self.store.dir / "models.json"
        if models is None and not models_path.exists():
            models_path.write_text(
                json.dumps(DEFAULT_MODELS_CONFIG, indent=2) + "\n", encoding="utf-8"
            )

        self.kernel = Kernel(
            policy=policy,
            slots=slots,
            store=self.store,
            tick=tick,
            daemon=True,
            recover=recover,
            models=models,
            permissions=permissions,
            tools=tools,
        )
        # Bind synchronously so self.url is real before start() is awaited.
        self.server = make_server(self, host, port)
        bound_host, bound_port = self.server.server_address[:2]
        self.url = f"http://{bound_host}:{bound_port}"
        self.loop: asyncio.AbstractEventLoop | None = None

    async def start(self) -> None:
        self.loop = asyncio.get_running_loop()
        endpoint = self.store.dir / "daemon.json"
        # The token goes in the endpoint file so a local client needs no config.
        endpoint.write_text(
            json.dumps({
                "url": self.url,
                "os_pid": os.getpid(),
                **({"token": self.token} if self.token else {}),
            }),
            encoding="utf-8",
        )
        try:  # best effort: not every filesystem honours this
            endpoint.chmod(0o600)
        except OSError:
            pass
        threading.Thread(
            target=self.server.serve_forever, daemon=True, name="agentos-api"
        ).start()
        try:
            await self.kernel.run()
        finally:
            # Agents are real OS processes: they would be orphaned otherwise.
            tasks = [
                p.task
                for p in self.kernel.table.all()
                if p.task is not None and not p.task.done()
            ]
            for t in tasks:
                t.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            self.server.shutdown()
            endpoint.unlink(missing_ok=True)

    def stop(self) -> None:
        """Ask the kernel loop to exit. Callable from any thread."""
        if self.loop is not None:
            self.loop.call_soon_threadsafe(setattr, self.kernel, "_shutdown", True)

    def call(self, fn: Callable[[], Any], timeout: float = 10.0) -> Any:
        """Run `fn` on the kernel's event loop and return its result."""
        fut: concurrent.futures.Future = concurrent.futures.Future()

        def runner() -> None:
            try:
                fut.set_result(fn())
            except BaseException as exc:
                fut.set_exception(exc)

        assert self.loop is not None, "daemon is not running"
        self.loop.call_soon_threadsafe(runner)
        return fut.result(timeout=timeout)
