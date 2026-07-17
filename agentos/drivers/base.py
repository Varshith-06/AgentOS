"""Tool drivers behave like device drivers (AgentOS.pdf p.6-7).

An agent never imports a tool library; it requests a capability by name and
the kernel dispatches to the driver that owns it. "Owns" is the operative word:
timeouts, rate limiting, retries, logging, and error handling live here, once,
instead of being reimplemented inside every agent.

A driver exposes its operations as `op_<name>` coroutines. execute() wraps
every call in the shared discipline: respect the rate limit, bound the runtime,
retry Transient failures, and convert anything unexpected into a ToolError the
agent can see and name — a driver bug must never take the kernel down.
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable


class ToolError(Exception):
    """The tool ran and failed, or refused to run. Agents see this message."""


class Transient(ToolError):
    """A failure worth retrying (a timeout upstream, a 5xx, a flaky link)."""


class ToolDriver:
    """Subclass, set `name`, and implement `op_<something>` coroutines."""

    name: str = "tool"
    timeout: float = 30.0  # seconds per attempt
    min_interval: float = 0.0  # rate limit: seconds between calls
    retries: int = 0  # extra attempts after a Transient failure

    def __init__(
        self,
        *,
        timeout: float | None = None,
        min_interval: float | None = None,
        retries: int | None = None,
        log: Callable[[str], None] | None = None,
        publish: Callable[..., Any] | None = None,
    ) -> None:
        if timeout is not None:
            self.timeout = timeout
        if min_interval is not None:
            self.min_interval = min_interval
        if retries is not None:
            self.retries = retries
        self._log = log or (lambda message: None)
        #: Drivers may announce kernel events (the filesystem driver publishes
        #: FileCreated, p.5). Drivers are kernel modules; agents are not.
        self._publish = publish or (lambda event_type, **payload: None)
        self._next_call = 0.0

    def ops(self) -> list[str]:
        return sorted(m[3:] for m in dir(self) if m.startswith("op_"))

    async def _respect_rate_limit(self) -> None:
        if self.min_interval <= 0:
            return
        loop = asyncio.get_running_loop()
        wait = self._next_call - loop.time()
        if wait > 0:
            self._log(f"{self.name}: rate limit, waiting {wait:.2f}s")
            await asyncio.sleep(wait)
        self._next_call = loop.time() + self.min_interval

    async def execute(self, op: str, params: dict[str, Any]) -> Any:
        handler = getattr(self, f"op_{op}", None)
        if handler is None:
            raise ToolError(
                f"{self.name!r} driver has no op {op!r} (ops: {', '.join(self.ops())})"
            )
        attempt = 0
        while True:
            attempt += 1
            await self._respect_rate_limit()
            try:
                return await asyncio.wait_for(handler(**params), self.timeout)
            except TimeoutError:
                raise ToolError(
                    f"{self.name}.{op} timed out after {self.timeout}s"
                ) from None
            except Transient as exc:
                if attempt > self.retries:
                    raise ToolError(
                        f"{self.name}.{op} failed after {attempt} attempt(s): {exc}"
                    ) from exc
                self._log(f"{self.name}.{op} attempt {attempt} failed: {exc}; retrying")
            except ToolError:
                raise
            except TypeError as exc:  # bad or missing params
                raise ToolError(f"{self.name}.{op} bad arguments: {exc}") from exc
            except Exception as exc:  # anything else: named, not propagated raw
                raise ToolError(
                    f"{self.name}.{op} failed: {type(exc).__name__}: {exc}"
                ) from exc
