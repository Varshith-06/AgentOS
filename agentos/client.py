"""The thin client (Phase 7, p.8).

Applications connect to a runtime that already exists instead of instantiating
one. An agent is submitted as its spec — module, class, params, all JSON — and
the daemon rebuilds and schedules it. The application owns nothing: no kernel,
no event loop, no process table. It can exit the moment it has submitted, and
the agent keeps running.

    from agentos.client import RuntimeClient

    client = RuntimeClient()               # finds .agentos/daemon.json
    pid = client.submit(MyAgent(topic="vector databases"))
    result = client.wait(pid)
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .agents.base import Agent, spec_of


class DaemonUnavailable(Exception):
    """No running daemon to talk to."""


class RemoteAgentFailed(Exception):
    """The submitted agent terminated as Failed; the reason is the message."""


class RuntimeClient:
    def __init__(self, url: str | None = None, dirpath: str | Path = ".agentos") -> None:
        if url is None:
            endpoint = Path(dirpath) / "daemon.json"
            if not endpoint.exists():
                raise DaemonUnavailable(
                    f"no daemon endpoint at {endpoint}. "
                    "Start one: python -m agentos.cli daemon"
                )
            url = json.loads(endpoint.read_text(encoding="utf-8"))["url"]
        self.url = url.rstrip("/")

    # -- transport -----------------------------------------------------------
    def _request(self, method: str, path: str, body: dict | None = None) -> Any:
        data = None if body is None else json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            self.url + path,
            data=data,
            headers={"Content-Type": "application/json"},
            method=method,
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                detail = json.loads(exc.read().decode("utf-8")).get("error", "")
            except Exception:
                detail = ""
            raise RuntimeError(f"{exc.code} from {path}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise DaemonUnavailable(
                f"no runtime answering at {self.url}: {exc.reason}"
            ) from exc

    # -- submitting work -----------------------------------------------------
    def submit(
        self,
        agent: Agent,
        priority: str | None = None,
        grant: list[str] | None = None,
    ) -> int:
        """Hand the agent to the daemon. Returns its pid in the shared runtime.

        `grant` pins this agent's capabilities and becomes the ceiling for
        anything it goes on to create: no descendant can hold more. Without
        it the daemon's permission matrix decides by agent name, which is
        what a named, pre-declared agent wants.
        """
        spec = spec_of(agent)
        if priority is not None:
            spec["priority"] = priority
        body: dict[str, Any] = {"spec": spec}
        if grant is not None:
            body["grant"] = list(grant)
        return self._request("POST", "/agents", body)["pid"]

    def task(
        self,
        goal: str,
        tools: list[str] | None = None,
        model: str = "fast",
        **options: Any,
    ) -> int:
        """Submit work that has no predefined shape: a sentence and a tool list.

        The daemon spawns a planner, which invents whatever agents the goal
        needs — there is no graph and no agent classes to write. `tools` is
        the ceiling for the whole tree, and must fall within whatever the
        operator allowed with --task-tools. Returns the planner's pid; use
        wait(pid) for the result, or task_tree(pid) to see the team too.
        """
        body: dict[str, Any] = {
            "goal": goal, "tools": list(tools or []), "model": model
        }
        body.update(options)
        return self._request("POST", "/task", body)["pid"]

    def task_tree(self, pid: int) -> dict[str, Any]:
        """A task's status and result, plus every agent the planner created."""
        return self._request("GET", f"/task/{pid}")

    def status(self, pid: int) -> dict[str, Any]:
        return self._request("GET", f"/agents/{pid}")

    def wait(self, pid: int, timeout: float = 300.0, poll: float = 0.2) -> Any:
        """Block until the agent terminates; return its result."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            row = self.status(pid)
            if row["status"] == "Finished":
                return row["result"]
            if row["status"] == "Failed":
                raise RemoteAgentFailed(row["exit_reason"] or "failed")
            time.sleep(poll)
        raise TimeoutError(f"pid {pid} still {self.status(pid)['status']} after {timeout}s")

    def run(self, agent: Agent, timeout: float = 300.0) -> Any:
        return self.wait(self.submit(agent), timeout=timeout)

    # -- observing the shared runtime ----------------------------------------
    def health(self) -> dict[str, Any]:
        return self._request("GET", "/health")

    def ps(self) -> dict[str, Any]:
        """Everyone's agents — every application's — in one table (p.8)."""
        return self._request("GET", "/ps")

    def logs(self, limit: int = 200) -> list[dict[str, Any]]:
        return self._request("GET", f"/logs?limit={limit}")

    def events(self, limit: int = 200) -> list[dict[str, Any]]:
        return self._request("GET", f"/events?limit={limit}")

    # -- control -------------------------------------------------------------
    def kill(self, pid: int) -> None:
        self._request("POST", f"/agents/{pid}/kill")

    def pause(self, pid: int) -> None:
        self._request("POST", f"/agents/{pid}/pause")

    def resume(self, pid: int) -> None:
        self._request("POST", f"/agents/{pid}/resume")

    def approve(self, pid: int, role: str) -> None:
        self._request("POST", f"/agents/{pid}/approve", {"role": role})

    def shutdown(self) -> None:
        self._request("POST", "/shutdown")
