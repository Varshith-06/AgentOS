"""Agents as real OS processes (Phase 7, p.8).

Same executor interface as the asyncio Executor; the kernel cannot tell the
difference. Each agent gets its own interpreter running
`python -m agentos.runtime.child`, and Syscall and Reply cross to it as JSON
lines — the Phase 1 rule ("anything that survives json.dumps survives a
pipe") cashed in literally, which is why not a line of agents/ or kernel/
had to change.

The *transport* those JSON lines ride on is itself swappable:

  ProcessExecutor  stdio pipes. The child's real stdout is the channel.
  SocketExecutor   a loopback TCP socket. The executor owns one listening
                   socket; each child is handed a one-time token in its
                   environment, dials back, authenticates, and then speaks
                   the identical protocol. Nothing above this file knows
                   which transport carried a syscall.

The scheduler's discipline survives intact on both: the child only advances
when the kernel puts a reply on `proc.inbox` (which happens when the
scheduler grants a slot), so slots, pause-at-syscall, replay after a crash —
all of it works on subprocess agents unchanged. And kill() is literal:
cancelling the pump task kills the child process.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import sys
from collections import deque
from typing import Any, Callable

from ..agents.base import spec_of
from ..kernel.messages import Syscall
from ..kernel.process import AgentProcess

CONNECT_TIMEOUT = 30.0  # seconds a spawned child gets to dial back (socket)


class ProcessExecutor:
    """Owns one OS subprocess per agent. Syscall transport: stdio pipes."""

    transport = "pipe"

    def __init__(
        self,
        mailbox: asyncio.Queue,
        on_finish: Callable[[AgentProcess, Any], None],
        on_fail: Callable[[AgentProcess, BaseException], None],
    ) -> None:
        self._mailbox = mailbox
        self._on_finish = on_finish
        self._on_fail = on_fail

    def start(self, proc: AgentProcess, agent: Any) -> asyncio.Task:
        task = asyncio.create_task(
            self._run(proc, agent), name=f"agent-proc-{proc.pid}"
        )
        proc.task = task
        return task

    # -- transport hooks (overridden by SocketExecutor) ----------------------
    async def _spawn(self) -> tuple[Any, Any]:
        """Exec the child interpreter. Returns (child, ticket) where ticket is
        whatever _connect needs to find this child's channel again."""
        child = await asyncio.create_subprocess_exec(
            sys.executable,
            "-X", "utf8",
            "-m", "agentos.runtime.child",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        return child, None

    async def _connect(self, child: Any, ticket: Any):
        """Return (reader, writer) for the syscall channel of this child."""
        return child.stdout, child.stdin

    # -- the common message loop ---------------------------------------------
    async def _run(self, proc: AgentProcess, agent: Any) -> None:
        stderr_tail: deque[str] = deque(maxlen=40)
        child = None  # a kill can land before the interpreter even exists
        writer = None
        pumps: list[asyncio.Task] = []
        try:
            child, ticket = await self._spawn()
            reader, writer = await self._connect(child, ticket)

            header = {"pid": proc.pid, "name": proc.name, "spec": spec_of(agent)}
            writer.write((json.dumps(header) + "\n").encode("utf-8"))
            await writer.drain()

            pumps = [
                asyncio.create_task(self._feed_replies(proc, writer)),
                asyncio.create_task(self._collect_stderr(child, stderr_tail)),
            ]

            while True:
                line = await reader.readline()
                if not line:
                    code = await child.wait()
                    detail = " | ".join(stderr_tail) or "no stderr"
                    raise RuntimeError(
                        f"agent process died (exit {code}): {detail[-400:]}"
                    )
                msg = json.loads(line)
                if msg["type"] == "syscall":
                    await self._mailbox.put(
                        Syscall(
                            pid=proc.pid,
                            op=msg["op"],
                            req_id=msg["req_id"],
                            args=msg["args"],
                        )
                    )
                elif msg["type"] == "finished":
                    self._on_finish(proc, msg["result"])
                    return
                elif msg["type"] == "failed":
                    self._on_fail(proc, RuntimeError(msg["error"]))
                    return
        except asyncio.CancelledError:
            # Killed by the kernel — and with process isolation, "killed"
            # means the OS process actually dies.
            if child is not None:
                child.kill()
                await child.wait()
            self._on_fail(proc, asyncio.CancelledError("killed"))
            raise
        except Exception as exc:  # protocol or transport failure
            self._on_fail(proc, exc)
        finally:
            for pump in pumps:
                pump.cancel()
            if writer is not None:
                writer.close()
            if child is not None:
                if child.returncode is None:
                    child.kill()
                try:
                    await asyncio.shield(child.wait())
                except asyncio.CancelledError:
                    pass
                self._close_pipes(child)

    @staticmethod
    def _close_pipes(child) -> None:
        """Close the child's pipe transports explicitly.

        Left to the garbage collector, Windows' proactor loop complains about
        unclosed transports at interpreter shutdown — and its warning path
        itself raises on an already-closed pipe, so every run ends in a wall
        of ValueError tracebacks that look like a crash and are not. Closing
        here is the fix; the try is because a transport that has already gone
        is exactly the state we want.
        """
        transport = getattr(child, "_transport", None)
        if transport is None:
            return
        try:
            transport.close()
        except (OSError, ValueError, AttributeError):
            pass

    async def _feed_replies(self, proc: AgentProcess, writer) -> None:
        """Kernel replies land on proc.inbox exactly as they always did; this
        pump is the only part that knows the inbox now ends at a transport."""
        while True:
            reply = await proc.inbox.get()
            line = json.dumps(
                {"req_id": reply.req_id, "value": reply.value, "error": reply.error}
            )
            writer.write((line + "\n").encode("utf-8"))
            await writer.drain()

    @staticmethod
    async def _collect_stderr(child, tail: deque) -> None:
        while True:
            line = await child.stderr.readline()
            if not line:
                return
            tail.append(line.decode("utf-8", errors="replace").rstrip())


class SocketExecutor(ProcessExecutor):
    """ProcessExecutor with the syscall channel over loopback TCP.

    One listening socket for the whole executor, opened lazily on the first
    spawn, bound to 127.0.0.1 on an ephemeral port. Each child receives the
    endpoint and a single-use token via its environment (AGENTOS_CONNECT,
    AGENTOS_TOKEN), connects, sends {"token": ...} as its first line, and from
    then on the wire format is byte-identical to the pipe transport. A
    connection with an unknown, reused, or missing token is dropped.

    The channel is a persistent full-duplex stream, deliberately not HTTP:
    replies arrive when the scheduler grants a slot, not as responses to
    requests. (The daemon's control plane is HTTP; this is the data plane.)
    """

    transport = "socket"

    def __init__(self, *args: Any, **kw: Any) -> None:
        super().__init__(*args, **kw)
        self._server: asyncio.Server | None = None
        self._port: int | None = None
        #: token -> Future[(reader, writer)] for children we expect to dial in
        self._expected: dict[str, asyncio.Future] = {}

    async def _ensure_server(self) -> None:
        if self._server is None:
            self._server = await asyncio.start_server(
                self._accept, "127.0.0.1", 0
            )
            self._port = self._server.sockets[0].getsockname()[1]

    async def _accept(self, reader, writer) -> None:
        try:
            hello = await asyncio.wait_for(reader.readline(), timeout=10)
            token = json.loads(hello).get("token")
        except Exception:
            writer.close()
            return
        fut = self._expected.pop(token, None) if token else None
        if fut is None or fut.done():
            writer.close()
            return
        fut.set_result((reader, writer))

    async def _spawn(self) -> tuple[Any, Any]:
        await self._ensure_server()
        token = secrets.token_hex(16)
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._expected[token] = fut
        env = dict(
            os.environ,
            AGENTOS_CONNECT=f"127.0.0.1:{self._port}",
            AGENTOS_TOKEN=token,
        )
        child = await asyncio.create_subprocess_exec(
            sys.executable,
            "-X", "utf8",
            "-m", "agentos.runtime.child",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        return child, (token, fut)

    async def _connect(self, child: Any, ticket: Any):
        token, fut = ticket
        died = asyncio.ensure_future(child.wait())
        try:
            done, _ = await asyncio.wait(
                {fut, died},
                timeout=CONNECT_TIMEOUT,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if fut in done:
                died.cancel()
                return fut.result()
            if died in done:
                detail = (await child.stderr.read()).decode(
                    "utf-8", errors="replace"
                ).strip() or "no stderr"
                raise RuntimeError(
                    f"agent process died before connecting "
                    f"(exit {died.result()}): {detail[-400:]}"
                )
            raise RuntimeError(
                f"agent process did not connect within {CONNECT_TIMEOUT:.0f}s"
            )
        finally:
            self._expected.pop(token, None)
            if not fut.done():
                fut.cancel()

    async def aclose(self) -> None:
        """Stop listening. Running channels are unaffected; only new spawns
        would need the server, and the kernel is shutting down."""
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
