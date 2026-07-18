"""The shared runtime daemon (Phase 7, p.8).

The runtime becomes a process that outlives any application. Applications are
thin clients: they connect to a runtime that already exists, submit agents as
specs, and walk away — the daemon owns scheduling, permissions, memory,
models, journaling, and recovery for everyone's agents at once. One
`agent ps` shows them all, with cost aggregated across applications. That is
the claim that separates AgentOS from a library.

    python -m agentos.cli daemon                 # terminal 1: the runtime
    python examples/app_research.py              # terminal 2: an application
    python examples/app_support.py               # terminal 3: another one
    python -m agentos.cli ps                     # everyone, one table
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import os
import threading
from typing import Any, Callable

from ..api import make_server
from ..kernel.kernel import Kernel
from ..kernel.models import DEFAULT_MODELS_CONFIG
from ..kernel.store import Store


class Daemon:
    def __init__(
        self,
        store: Store | None = None,
        dirpath: str = ".agentos",
        host: str = "127.0.0.1",
        port: int = 7070,
        policy: str = "fifo",
        slots: int = 4,
        isolation: str = "process",
        transport: str = "socket",
        tick: float = 0.05,
        recover: bool = False,
        models: Any = None,
        permissions: Any = None,
        tools: dict[str, dict[str, Any]] | None = None,
        task_tools: list[str] | None = None,
    ) -> None:
        self.store = store if store is not None else Store(dirpath)
        #: What POST /task is allowed to grant. The operator decides this when
        #: starting the runtime; a caller may request any subset and nothing
        #: outside it. Empty means submitted tasks get no tools at all, which
        #: is the right default for an endpoint that accepts a sentence from
        #: the network and builds a team out of it.
        self.task_tools: set[str] = set(task_tools or ())

        # First boot convenience: a daemon with no routing table would refuse
        # every request_model, so seed the default chain (frontier -> local ->
        # mock). Editing the file afterwards is the whole point of Phase 5.
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
            isolation=isolation,
            transport=transport,
            daemon=True,
            recover=recover,
            models=models,
            permissions=permissions,
            # Driver configuration belongs to the operator, not the caller:
            # the filesystem root a hosted runtime sandboxes agents to is
            # exactly the sort of thing a submitted task must not choose.
            tools=tools,
        )
        # Bind synchronously so self.url is real before start() is awaited.
        self.server = make_server(self, host, port)
        bound_host, bound_port = self.server.server_address[:2]
        self.url = f"http://{bound_host}:{bound_port}"
        self.loop: asyncio.AbstractEventLoop | None = None

    # -- lifecycle -----------------------------------------------------------
    async def start(self) -> None:
        self.loop = asyncio.get_running_loop()
        endpoint = self.store.dir / "daemon.json"
        endpoint.write_text(
            json.dumps({"url": self.url, "os_pid": os.getpid()}), encoding="utf-8"
        )
        threading.Thread(
            target=self.server.serve_forever, daemon=True, name="agentos-api"
        ).start()
        try:
            await self.kernel.run()  # forever, until stop()
        finally:
            # Take the children with us — with process isolation these are
            # real OS processes that would otherwise be orphaned.
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

    # -- the bridge HTTP threads use -----------------------------------------
    def call(self, fn: Callable[[], Any], timeout: float = 10.0) -> Any:
        """Run `fn` on the kernel's event loop and return its result.

        Kernel state is only ever touched from the loop thread; this is the
        one door in, and every mutating API route goes through it.
        """
        fut: concurrent.futures.Future = concurrent.futures.Future()

        def runner() -> None:
            try:
                fut.set_result(fn())
            except BaseException as exc:
                fut.set_exception(exc)

        assert self.loop is not None, "daemon is not running"
        self.loop.call_soon_threadsafe(runner)
        return fut.result(timeout=timeout)
