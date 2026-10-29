"""Model routing (AgentOS.pdf p.7)."""

from __future__ import annotations

import asyncio
import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_MAX_TOKENS = 1024
DEFAULT_TIMEOUT = 120.0

# Candidates in preference order. Prices are USD per million tokens
# (input, output).
DEFAULT_MODELS_CONFIG: dict[str, Any] = {
    "classes": {
        "fast": [
            {
                "provider": "openai",
                "base_url": "https://api.groq.com/openai/v1",
                "api_key_env": "GROQ_API_KEY",
                "model": "openai/gpt-oss-120b",
                "cost_per_mtok": [0.15, 0.75],
                "context_window": 131072,
            },
            {
                "provider": "openai",
                "base_url": "https://openrouter.ai/api/v1",
                "api_key_env": "OPENROUTER_API_KEY",
                "model": "openai/gpt-oss-120b",
                "cost_per_mtok": [0.15, 0.75],
                "context_window": 131072,
            },
            {
                "provider": "openai",
                "base_url": "http://localhost:11434/v1",
                "model": "gpt-oss:120b",
                "api_key_env": None,
                "cost_per_mtok": [0, 0],
                "context_window": 131072,
            },
            {"provider": "mock", "model": "mock-fast"},
        ],
        "reasoning": [
            {
                "provider": "openai",
                "base_url": "https://api.groq.com/openai/v1",
                "api_key_env": "GROQ_API_KEY",
                "model": "openai/gpt-oss-120b",
                "cost_per_mtok": [0.15, 0.75],
                "context_window": 131072,
                "params": {"reasoning_effort": "high"},
            },
            {
                "provider": "openai",
                "base_url": "https://openrouter.ai/api/v1",
                "api_key_env": "OPENROUTER_API_KEY",
                "model": "openai/gpt-oss-120b",
                "cost_per_mtok": [0.15, 0.75],
                "context_window": 131072,
                "params": {"reasoning_effort": "high"},
            },
            {"provider": "mock", "model": "mock-reasoning"},
        ],
    }
}


class ModelError(Exception):
    """No candidate could serve the request. Agents see this message."""


class ModelManager:
    def __init__(
        self,
        classes: dict[str, list[dict[str, Any]]] | None = None,
        path: Path | str | None = None,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self.classes = classes or {}
        self.path = Path(path) if path is not None else None
        self._log = log or (lambda message: None)
        self._script_pos: dict[int, int] = {}
        if self.path is not None and self.path.exists():
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.classes = data.get("classes", data)

    @classmethod
    def of(cls, source: Any, default_path: Path, log=None) -> "ModelManager":
        if isinstance(source, ModelManager):
            return source
        if isinstance(source, dict):
            return cls(classes=source.get("classes", source), log=log)
        if isinstance(source, (str, Path)):
            return cls(path=source, log=log)
        return cls(path=default_path, log=log)

    def idempotent(self, need: str) -> bool:
        """Does every candidate for `need` honour Idempotency-Key?

        A candidate says so with `"idempotent": true`. That is true of a
        provider behind an AgentOS gateway (`agent gateway`), and of no model
        provider on its own today."""
        candidates = self.classes.get(need) or []
        return bool(candidates) and all(c.get("idempotent") for c in candidates)

    async def request(
        self,
        need: str,
        prompt: str,
        system: str | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        idempotency_key: str | None = None,
        courier: Callable[..., Any] | None = None,
    ) -> dict[str, Any]:
        """Serve `need` from the first candidate that answers.

        `courier(target, args, meta)`, if given, makes each HTTP call in a
        process of its own (agentos.runtime.courier) instead of this one.
        """
        candidates = self.classes.get(need)
        if not candidates:
            have = ", ".join(sorted(self.classes)) or "none"
            raise ModelError(
                f"no models configured for need {need!r} (configured: {have}). "
                f"Add it to {self.path or 'the models config'}."
            )

        estimated = _estimate_tokens(prompt) + _estimate_tokens(system or "")
        candidates = self._rank(candidates, estimated)
        failures: list[str] = []
        for cand in candidates:
            label = f"{cand.get('provider', '?')}:{cand.get('model', '?')}"
            reason = self._unavailable(cand)
            if reason is not None:
                failures.append(f"{label}: {reason}")
                continue
            window = cand.get("context_window")
            if window is not None and estimated > window:
                failures.append(f"{label}: prompt ~{estimated} tokens > window {window}")
                continue
            try:
                loop = asyncio.get_running_loop()
                started = loop.time()
                raw = await asyncio.wait_for(
                    self._call(cand, prompt, system, max_tokens, idempotency_key, courier),
                    cand.get("timeout", DEFAULT_TIMEOUT),
                )
                return self.result(cand, raw, loop.time() - started)
            except Exception as exc:
                failures.append(f"{label}: {type(exc).__name__}: {exc}")
                self._log(f"{label} failed, trying next candidate: {exc}")
        raise ModelError(
            f"no available model for need {need!r}: " + "; ".join(failures)
        )

    @staticmethod
    def result(cand: dict[str, Any], raw: dict[str, Any], latency: float = 0.0) -> dict[str, Any]:
        """What an agent is told about one served request: the reply, priced."""
        rates = cand.get("cost_per_mtok") or [0.0, 0.0]
        return {
            "text": raw["text"],
            "model": cand.get("model", "?"),
            "provider": cand.get("provider", "?"),
            "input_tokens": raw["input_tokens"],
            "output_tokens": raw["output_tokens"],
            "cost": raw["input_tokens"] * rates[0] / 1e6
            + raw["output_tokens"] * rates[1] / 1e6,
            "latency": round(latency, 3),
            # What a deduplicating gateway said about the call, if one did.
            "replayed": raw.get("replayed", False),
            "outcome_unknown": raw.get("outcome_unknown", False),
        }

    def _rank(self, candidates: list[dict], estimated: int) -> list[dict]:
        """Order the candidates by the p.7 selection criteria."""
        if not isinstance(candidates, dict):
            prefer, items = None, list(candidates)
        else:
            prefer = candidates.get("prefer")
            items = list(candidates.get("candidates", []))
        if not prefer or prefer == "order":
            return items

        def projected_cost(c: dict) -> float:
            rates = c.get("cost_per_mtok") or [0.0, 0.0]
            # Output length is unknown before the call; assume it mirrors the prompt.
            return (estimated * rates[0] + estimated * rates[1]) / 1e6

        keys = {
            "cheapest": projected_cost,
            "fastest": lambda c: c.get("latency", 0.0),
            "best": lambda c: -float(c.get("quality", 0)),
        }
        key = keys.get(prefer)
        if key is None:
            self._log(f"unknown prefer={prefer!r}; using config order")
            return items
        fits = [c for c in items if self._fits(c, estimated)]
        rest = [c for c in items if not self._fits(c, estimated)]
        return sorted(fits, key=key) + rest

    @staticmethod
    def _fits(cand: dict, estimated: int) -> bool:
        window = cand.get("context_window")
        return window is None or estimated <= window

    def _unavailable(self, cand: dict[str, Any]) -> str | None:
        provider = cand.get("provider")
        if provider == "mock":
            return None
        if provider == "litellm":
            try:
                import litellm  # noqa: F401
            except ImportError:
                return "litellm is not installed"
            return None
        if provider == "anthropic":
            env = cand.get("api_key_env", "ANTHROPIC_API_KEY")
            return None if os.environ.get(env) else f"{env} is not set"
        if provider == "openai":
            env = cand.get(
                "api_key_env", None if "base_url" in cand else "OPENAI_API_KEY"
            )
            if env and not os.environ.get(env):
                return f"{env} is not set"
            return None
        return f"unknown provider {provider!r}"

    async def _call(
        self, cand: dict[str, Any], prompt: str, system: str | None, max_tokens: int,
        idempotency_key: str | None = None, courier: Callable[..., Any] | None = None,
    ) -> dict[str, Any]:
        provider = cand["provider"]
        if courier is not None and provider in ("openai", "anthropic"):
            return await courier(
                "agentos.kernel.models:http_call",
                {"cand": cand, "prompt": prompt, "system": system,
                 "max_tokens": max_tokens, "idempotency_key": idempotency_key},
                {"cand": cand},  # enough to price the reply when it is adopted
            )
        if provider == "mock":
            return await self._call_mock(cand, prompt, system, max_tokens)
        if provider == "anthropic":
            return await self._call_anthropic(
                cand, prompt, system, max_tokens, idempotency_key
            )
        if provider == "openai":
            return await asyncio.to_thread(
                self._openai_http, cand, prompt, system, max_tokens, idempotency_key
            )
        if provider == "litellm":
            return await self._call_litellm(cand, prompt, system, max_tokens)
        raise ModelError(f"unknown provider {provider!r}")

    async def _call_mock(
        self, cand: dict[str, Any], prompt: str, system: str | None, max_tokens: int
    ) -> dict[str, Any]:
        if cand.get("simulate_failure"):
            raise ConnectionError("simulated provider outage")
        await asyncio.sleep(cand.get("latency", 0.02))
        words = len(prompt.split())
        text = cand.get("reply") or (
            f"[{cand.get('model', 'mock')}] deterministic offline reply "
            f"to your {words}-word prompt."
        )
        # A `script` makes the mock a fake reasoner: successive calls get successive
        # replies, and the last entry repeats.
        script = cand.get("script")
        if script:
            key = id(cand)
            i = self._script_pos.get(key, 0)
            text = script[min(i, len(script) - 1)]
            self._script_pos[key] = i + 1
            if not isinstance(text, str):
                text = json.dumps(text)
        return {
            "text": text,
            "input_tokens": words + len((system or "").split()),
            "output_tokens": len(text.split()),
        }

    async def _call_anthropic(
        self, cand: dict[str, Any], prompt: str, system: str | None, max_tokens: int,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        key = os.environ[cand.get("api_key_env", "ANTHROPIC_API_KEY")]
        body, extra = _anthropic_request(cand, prompt, system, max_tokens, idempotency_key)
        if cand.get("base_url") or extra:
            # A gateway in front, or a key to send: plain HTTP, whose reply
            # headers say whether the gateway replayed the call.
            return await asyncio.to_thread(
                self._anthropic_http, key, body, extra, cand.get("base_url")
            )
        try:
            import anthropic
        except ImportError:
            return await asyncio.to_thread(self._anthropic_http, key, body)
        client = anthropic.AsyncAnthropic(api_key=key)
        resp = await client.messages.create(**body)
        return {
            "text": "".join(b.text for b in resp.content if b.type == "text"),
            "input_tokens": resp.usage.input_tokens,
            "output_tokens": resp.usage.output_tokens,
        }

    def _anthropic_http(
        self, key: str, body: dict[str, Any], extra: dict[str, str] | None = None,
        base_url: str | None = None,
    ) -> dict[str, Any]:
        seen: dict[str, str] = {}
        url = f"{base_url.rstrip('/')}/messages" if base_url else ANTHROPIC_URL
        data = _post_json(
            url,
            body,
            {"x-api-key": key, "anthropic-version": ANTHROPIC_VERSION, **(extra or {})},
            seen,
        )
        return {
            **_gateway_flags(seen),
            "text": "".join(
                b.get("text", "") for b in data["content"] if b["type"] == "text"
            ),
            "input_tokens": data["usage"]["input_tokens"],
            "output_tokens": data["usage"]["output_tokens"],
        }

    def _openai_http(
        self, cand: dict[str, Any], prompt: str, system: str | None, max_tokens: int,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        base = cand.get("base_url", "https://api.openai.com/v1").rstrip("/")
        env = cand.get("api_key_env", None if "base_url" in cand else "OPENAI_API_KEY")
        headers = {}
        if env and os.environ.get(env):
            headers["Authorization"] = f"Bearer {os.environ[env]}"
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        seen: dict[str, str] = {}
        messages = [{"role": "system", "content": system}] if system else []
        messages.append({"role": "user", "content": prompt})
        data = _post_json(
            f"{base}/chat/completions",
            {
                "model": cand["model"],
                "messages": messages,
                "max_tokens": max_tokens,
                **cand.get("params", {}),
            },
            headers,
            seen,
        )
        usage = data.get("usage") or {}
        text = (data["choices"][0]["message"].get("content") or "").strip()
        return {
            **_gateway_flags(seen),
            "text": text,
            "input_tokens": usage.get("prompt_tokens", _estimate_tokens(prompt)),
            "output_tokens": usage.get("completion_tokens", _estimate_tokens(text)),
        }

    async def _call_litellm(
        self, cand: dict[str, Any], prompt: str, system: str | None, max_tokens: int
    ) -> dict[str, Any]:
        import litellm

        messages = [{"role": "system", "content": system}] if system else []
        messages.append({"role": "user", "content": prompt})
        resp = await litellm.acompletion(
            model=cand["model"], messages=messages, max_tokens=max_tokens,
            **cand.get("params", {}),
        )
        usage = getattr(resp, "usage", None)
        return {
            "text": resp.choices[0].message.content or "",
            "input_tokens": getattr(usage, "prompt_tokens", 0) or 0,
            "output_tokens": getattr(usage, "completion_tokens", 0) or 0,
        }


def http_call(
    cand: dict[str, Any], prompt: str, system: str | None, max_tokens: int,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """One candidate's HTTP call, as a courier makes it: a process of its own,
    the API key from the environment it inherited."""
    manager = ModelManager()
    if cand["provider"] == "openai":
        return manager._openai_http(cand, prompt, system, max_tokens, idempotency_key)
    key = os.environ[cand.get("api_key_env", "ANTHROPIC_API_KEY")]
    body, extra = _anthropic_request(cand, prompt, system, max_tokens, idempotency_key)
    return manager._anthropic_http(key, body, extra, cand.get("base_url"))


def _anthropic_request(
    cand: dict[str, Any], prompt: str, system: str | None, max_tokens: int,
    idempotency_key: str | None,
) -> tuple[dict[str, Any], dict[str, str]]:
    """The Messages API body, and the extra headers it is sent with."""
    extra = {"Idempotency-Key": idempotency_key} if idempotency_key else {}
    body: dict[str, Any] = {
        "model": cand["model"],
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
        **cand.get("params", {}),
    }
    if system:
        body["system"] = system
    return body, extra


def _estimate_tokens(text: str) -> int:
    """A rough pre-flight size check for context-window routing only."""
    return max(len(text) // 4, len(text.split()))


def _gateway_flags(seen: dict[str, str]) -> dict[str, bool]:
    return {
        "replayed": seen.get("idempotent-replayed") == "true",
        "outcome_unknown": seen.get("idempotency-outcome") == "unknown",
    }


def _post_json(
    url: str, body: dict[str, Any], headers: dict[str, str],
    seen: dict[str, str] | None = None,
) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        # Several providers sit behind Cloudflare, which rejects urllib's default
        # User-Agent outright (403, code 1010). Groq does.
        headers={
            "Content-Type": "application/json",
            "User-Agent": "agentos/1.0",
            **headers,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=DEFAULT_TIMEOUT) as resp:
            if seen is not None:
                seen.update((k.lower(), v) for k, v in resp.headers.items())
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        raise ConnectionError(f"{exc.code} from {url}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise ConnectionError(f"cannot reach {url}: {exc.reason}") from exc
