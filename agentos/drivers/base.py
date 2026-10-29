"""Tool drivers behave like device drivers (AgentOS.pdf p.6-7)."""

from __future__ import annotations

import asyncio
import inspect
import json
import re
import time
from typing import Any, Callable

_JSON_TYPES = {"str": "string", "int": "integer", "float": "number", "bool": "boolean",
               "list": "array", "dict": "object"}


def _schema_of(annotation: Any) -> dict[str, Any]:
    """The JSON Schema type of a parameter annotation; {} when it is anything."""
    if annotation is inspect.Parameter.empty:
        return {}
    text = annotation if isinstance(annotation, str) else getattr(
        annotation, "__name__", str(annotation))
    text = re.sub(r"\[[^\]]*\]", "", text)  # list[str] is a list; the element type is detail
    kinds = [_JSON_TYPES[t] for t in re.findall(r"\b(str|int|float|bool|list|dict)\b", text)]
    kinds = list(dict.fromkeys(kinds))
    if not kinds:
        return {}
    return {"type": kinds[0] if len(kinds) == 1 else kinds}


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
    cache_ttl: float = 0.0  # seconds a result stays fresh; 0 disables caching
    # Ops safe to serve from cache; caching a write would be a correctness bug.
    cacheable: tuple[str, ...] = ()
    # Ops that take an `idempotency_key` and apply its effect at most once per
    # key. Only these can be re-issued after a crash without risking a repeat.
    idempotent: tuple[str, ...] = ()
    # Ops a courier can make in a process of its own (op -> 'module:function',
    # called with the op's params): a kernel with couriers on sends them that
    # way, so a runtime crash mid-call loses neither the call nor its reply.
    courier: dict[str, str] = {}

    def __init__(
        self,
        *,
        timeout: float | None = None,
        min_interval: float | None = None,
        retries: int | None = None,
        cache_ttl: float | None = None,
        log: Callable[[str], None] | None = None,
        publish: Callable[..., Any] | None = None,
    ) -> None:
        if timeout is not None:
            self.timeout = timeout
        if min_interval is not None:
            self.min_interval = min_interval
        if retries is not None:
            self.retries = retries
        if cache_ttl is not None:
            self.cache_ttl = cache_ttl
        self._cache: dict[str, tuple[float, Any]] = {}
        self._log = log or (lambda message: None)
        self._publish = publish or (lambda event_type, **payload: None)
        self._next_call = 0.0

    def ops(self) -> list[str]:
        return sorted(m[3:] for m in dir(self) if m.startswith("op_"))

    async def describe(self) -> dict[str, Any]:
        """op -> its description and a JSON Schema of its params, read off the
        op's signature, so a model need not guess what an op takes."""
        out = {}
        for op in self.ops():
            handler = getattr(self, f"op_{op}")
            doc = inspect.getdoc(handler) or ""
            props: dict[str, Any] = {}
            required = []
            for p in inspect.signature(handler).parameters.values():
                if p.kind in (p.VAR_KEYWORD, p.VAR_POSITIONAL) or p.name == "idempotency_key":
                    continue  # the kernel supplies the key
                props[p.name] = _schema_of(p.annotation)
                if p.default is p.empty:
                    required.append(p.name)
            out[op] = {
                "description": doc.split("\n\n")[0].replace("\n", " "),
                "params": {"type": "object", "properties": props, "required": required},
            }
        return out

    async def _respect_rate_limit(self) -> None:
        if self.min_interval <= 0:
            return
        loop = asyncio.get_running_loop()
        wait = self._next_call - loop.time()
        if wait > 0:
            self._log(f"{self.name}: rate limit, waiting {wait:.2f}s")
            await asyncio.sleep(wait)
        self._next_call = loop.time() + self.min_interval

    def _cache_key(self, op: str, params: dict[str, Any]) -> str | None:
        """None means "do not cache this call"."""
        if self.cache_ttl <= 0 or op not in self.cacheable:
            return None
        try:
            return f"{op}:{json.dumps(params, sort_keys=True, default=str)}"
        except (TypeError, ValueError):
            return None  # unhashable params: run it, do not guess

    def _cached(self, key: str | None) -> tuple[bool, Any]:
        if key is None:
            return False, None
        hit = self._cache.get(key)
        if hit is None:
            return False, None
        expires, value = hit
        if expires < time.monotonic():
            del self._cache[key]
            return False, None
        return True, value

    async def execute(
        self, op: str, params: dict[str, Any], idempotency_key: str | None = None,
        via: Callable[[dict[str, Any]], Any] | None = None,
    ) -> Any:
        """Run one op. `via`, if given, makes the call instead of the op's own
        coroutine (a courier); timeouts, retries and the cache apply as ever."""
        handler = getattr(self, f"op_{op}", None)
        if handler is None:
            raise ToolError(
                f"{self.name!r} driver has no op {op!r} (ops: {', '.join(self.ops())})"
            )
        if idempotency_key is not None and op in self.idempotent:
            params = {**params, "idempotency_key": idempotency_key}
        key = self._cache_key(op, params)
        fresh, value = self._cached(key)
        if fresh:
            self._log(f"{self.name}.{op} served from cache")
            return value
        attempt = 0
        while True:
            attempt += 1
            await self._respect_rate_limit()
            try:
                result = await asyncio.wait_for(
                    via(params) if via is not None else handler(**params), self.timeout
                )
                if key is not None:
                    self._cache[key] = (time.monotonic() + self.cache_ttl, result)
                return result
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
            except TypeError as exc:
                raise ToolError(f"{self.name}.{op} bad arguments: {exc}") from exc
            except Exception as exc:
                raise ToolError(
                    f"{self.name}.{op} failed: {type(exc).__name__}: {exc}"
                ) from exc
