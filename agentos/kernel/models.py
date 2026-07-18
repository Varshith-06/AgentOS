"""Model routing (AgentOS.pdf p.7).

Agents request a capability class — "Need: fast", "Need: reasoning" — and
never a model name. The routing table lives in `.agentos/models.json`:

    {
      "classes": {
        "fast": [
          {"provider": "anthropic", "model": "claude-haiku-4-5",
           "cost_per_mtok": [1.00, 5.00], "context_window": 200000},
          {"provider": "openai", "base_url": "http://localhost:11434/v1",
           "model": "llama3.2", "api_key_env": null},
          {"provider": "mock", "model": "mock-fast"}
        ]
      }
    }

Candidates are tried in order. One is skipped if it is unavailable (its API
key is not set) or the prompt exceeds its context window; one that fails at
call time (endpoint down, 5xx) falls through to the next. That is what makes
model choice a runtime configuration concern: set or unset a key, and the
same agent code lands on a different model.

Providers: `anthropic` (the official SDK when installed, stdlib HTTP
otherwise), `openai` (any OpenAI-compatible endpoint — OpenAI, Ollama, vLLM,
LM Studio), `litellm` (any model LiteLLM knows, if installed), and `mock`
(offline and deterministic, so the fake-agent examples keep working forever).
"""

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

#: The seeded routing table (examples, daemon first boot). Candidates in
#: preference order; unavailable ones are skipped, failing ones fall through.
#: Prices are USD per million tokens (input, output).
DEFAULT_MODELS_CONFIG: dict[str, Any] = {
    "classes": {
        "fast": [
            {
                "provider": "anthropic",
                "model": "claude-haiku-4-5",
                "cost_per_mtok": [1.00, 5.00],
                "context_window": 200000,
            },
            {
                "provider": "openai",
                "base_url": "http://localhost:11434/v1",
                "model": "llama3.2",
                "api_key_env": None,
                "cost_per_mtok": [0, 0],
            },
            {"provider": "mock", "model": "mock-fast"},
        ],
        "reasoning": [
            {
                "provider": "anthropic",
                "model": "claude-opus-4-8",
                "cost_per_mtok": [5.00, 25.00],
                "context_window": 1000000,
                "params": {"thinking": {"type": "adaptive"}},
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
        return cls(path=default_path, log=log)  # None: the standard location

    # -- routing -------------------------------------------------------------
    async def request(
        self,
        need: str,
        prompt: str,
        system: str | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> dict[str, Any]:
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
                    self._call(cand, prompt, system, max_tokens),
                    cand.get("timeout", DEFAULT_TIMEOUT),
                )
                rates = cand.get("cost_per_mtok") or [0.0, 0.0]
                return {
                    "text": raw["text"],
                    "model": cand.get("model", "?"),
                    "provider": cand.get("provider", "?"),
                    "input_tokens": raw["input_tokens"],
                    "output_tokens": raw["output_tokens"],
                    "cost": raw["input_tokens"] * rates[0] / 1e6
                    + raw["output_tokens"] * rates[1] / 1e6,
                    "latency": round(loop.time() - started, 3),
                }
            except Exception as exc:  # this candidate failed: try the next one
                failures.append(f"{label}: {type(exc).__name__}: {exc}")
                self._log(f"{label} failed, trying next candidate: {exc}")
        raise ModelError(
            f"no available model for need {need!r}: " + "; ".join(failures)
        )

    def _rank(self, candidates: list[dict], estimated: int) -> list[dict]:
        """Order the candidates by the p.7 selection criteria.

        The default stays "the order a human wrote them in", because that is
        the most honest expression of a preference and the config is where a
        preference belongs. Setting `prefer` on the class asks the runtime to
        decide instead:

            "reasoning": {"prefer": "cheapest", "candidates": [...]}

        cheapest  — lowest projected cost for this prompt, using the same
                    cost_per_mtok rates the ledger bills at
        fastest   — lowest declared latency
        best      — highest declared quality

        Candidates that cannot serve the prompt at all sort last rather than
        being dropped, so the failure message still names them.
        """
        if not isinstance(candidates, dict):
            prefer, items = None, list(candidates)
        else:
            prefer = candidates.get("prefer")
            items = list(candidates.get("candidates", []))
        if not prefer or prefer == "order":
            return items

        def projected_cost(c: dict) -> float:
            rates = c.get("cost_per_mtok") or [0.0, 0.0]
            # Output length is unknown before the call; assume it mirrors the
            # prompt. Wrong in detail, right in ordering, which is all rank needs.
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
            # A local endpoint (base_url given, api_key_env null) needs no key.
            env = cand.get(
                "api_key_env", None if "base_url" in cand else "OPENAI_API_KEY"
            )
            if env and not os.environ.get(env):
                return f"{env} is not set"
            return None
        return f"unknown provider {provider!r}"

    # -- transports ------------------------------------------------------------
    async def _call(
        self, cand: dict[str, Any], prompt: str, system: str | None, max_tokens: int
    ) -> dict[str, Any]:
        provider = cand["provider"]
        if provider == "mock":
            return await self._call_mock(cand, prompt, system, max_tokens)
        if provider == "anthropic":
            return await self._call_anthropic(cand, prompt, system, max_tokens)
        if provider == "openai":
            return await asyncio.to_thread(
                self._openai_http, cand, prompt, system, max_tokens
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
        return {
            "text": text,
            "input_tokens": words + len((system or "").split()),
            "output_tokens": len(text.split()),
        }

    async def _call_anthropic(
        self, cand: dict[str, Any], prompt: str, system: str | None, max_tokens: int
    ) -> dict[str, Any]:
        key = os.environ[cand.get("api_key_env", "ANTHROPIC_API_KEY")]
        body: dict[str, Any] = {
            "model": cand["model"],
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
            **cand.get("params", {}),
        }
        if system:
            body["system"] = system
        try:
            import anthropic  # the official SDK, when installed
        except ImportError:
            return await asyncio.to_thread(self._anthropic_http, key, body)
        client = anthropic.AsyncAnthropic(api_key=key)
        resp = await client.messages.create(**body)
        return {
            "text": "".join(b.text for b in resp.content if b.type == "text"),
            "input_tokens": resp.usage.input_tokens,
            "output_tokens": resp.usage.output_tokens,
        }

    def _anthropic_http(self, key: str, body: dict[str, Any]) -> dict[str, Any]:
        data = _post_json(
            ANTHROPIC_URL,
            body,
            {"x-api-key": key, "anthropic-version": ANTHROPIC_VERSION},
        )
        return {
            "text": "".join(
                b.get("text", "") for b in data["content"] if b["type"] == "text"
            ),
            "input_tokens": data["usage"]["input_tokens"],
            "output_tokens": data["usage"]["output_tokens"],
        }

    def _openai_http(
        self, cand: dict[str, Any], prompt: str, system: str | None, max_tokens: int
    ) -> dict[str, Any]:
        base = cand.get("base_url", "https://api.openai.com/v1").rstrip("/")
        env = cand.get("api_key_env", None if "base_url" in cand else "OPENAI_API_KEY")
        headers = {}
        if env and os.environ.get(env):
            headers["Authorization"] = f"Bearer {os.environ[env]}"
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
        )
        usage = data.get("usage") or {}
        text = (data["choices"][0]["message"].get("content") or "").strip()
        return {
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


def _estimate_tokens(text: str) -> int:
    """A rough pre-flight size check for context-window routing only.

    Never used for billing: real token counts come back from the provider.
    """
    return max(len(text) // 4, len(text.split()))


def _post_json(url: str, body: dict[str, Any], headers: dict[str, str]) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=DEFAULT_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        raise ConnectionError(f"{exc.code} from {url}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise ConnectionError(f"cannot reach {url}: {exc.reason}") from exc
