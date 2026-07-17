"""The REST driver: requests with retries on transient failures, stdlib only."""

from __future__ import annotations

import asyncio
import json as jsonlib
import urllib.error
import urllib.request
from typing import Any

from .base import ToolDriver, Transient


class Http(ToolDriver):
    name = "http"
    timeout = 15.0
    retries = 2  # 5xx and dropped connections are worth another try

    async def op_get(self, url: str, headers: dict | None = None) -> dict[str, Any]:
        return await asyncio.to_thread(self._fetch, "GET", url, None, headers or {})

    async def op_post(
        self, url: str, json: Any = None, headers: dict | None = None
    ) -> dict[str, Any]:
        body = None if json is None else jsonlib.dumps(json).encode("utf-8")
        hdrs = {"Content-Type": "application/json", **(headers or {})}
        return await asyncio.to_thread(self._fetch, "POST", url, body, hdrs)

    def _fetch(
        self, method: str, url: str, body: bytes | None, headers: dict
    ) -> dict[str, Any]:
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return {
                    "status": resp.status,
                    "body": resp.read().decode("utf-8", errors="replace"),
                }
        except urllib.error.HTTPError as exc:
            if exc.code >= 500:
                raise Transient(f"{exc.code} from {url}") from exc
            # A 4xx is an answer, not a failure: the agent decides what it means.
            return {
                "status": exc.code,
                "body": exc.read().decode("utf-8", errors="replace"),
            }
        except urllib.error.URLError as exc:
            raise Transient(f"cannot reach {url}: {exc.reason}") from exc
