"""The memory manager (AgentOS.pdf p.6).

Six kinds of memory behind four verbs — store, retrieve, share, delete — and
the backend stays invisible to the agent. Today it is SQLite; swapping in
Redis or a real vector database changes this file and nothing else.

The kinds, and who a row belongs to:

  working     private to one process; freed when it exits
  scratchpad  same, by convention for throwaway notes
  shared      one global namespace; readable by whoever the owner shared with
  longterm    keyed by agent *name*, so it survives restarts and new pids
  semantic    like longterm, plus a vector for similarity retrieval
  episodic    the agent's own execution history; the kernel writes it, agents
              may only read it

The semantic embedding is a deterministic hashed bag-of-words — deliberately
humble, like the browser driver. A real embedding model can replace _embed()
without any agent changing, because agents only ever say `query=...`.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
from typing import Any

from .store import Store

EPHEMERAL = ("working", "scratchpad", "shared")
PERSISTENT = ("longterm", "semantic")
KINDS = EPHEMERAL + PERSISTENT  # episodic is read-only and handled separately

_DIM = 128


def _embed(text: str) -> list[float]:
    """A stable, dependency-free placeholder embedding."""
    vec = [0.0] * _DIM
    for token in re.findall(r"[a-z0-9]+", text.lower()):
        digest = hashlib.md5(token.encode()).digest()
        vec[int.from_bytes(digest[:4], "big") % _DIM] += 1.0
    norm = math.sqrt(sum(v * v for v in vec))
    return [v / norm for v in vec] if norm else vec


def _similarity(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


class MemoryError_(Exception):
    """A memory operation the kernel refuses (bad kind, not the owner...)."""


class MemoryManager:
    def __init__(self, store: Store) -> None:
        self.store = store
        self.db = store.db

    # -- ownership ---------------------------------------------------------
    def _owner(self, proc: Any, kind: str) -> str:
        """Ephemeral memory dies with the pid; persistent memory follows the
        agent's name across restarts, because pids do not survive them."""
        return proc.name if kind in PERSISTENT else str(proc.pid)

    @staticmethod
    def _check_kind(kind: str) -> None:
        if kind not in KINDS:
            raise MemoryError_(
                f"unknown memory kind {kind!r} (have: {', '.join(KINDS)}, episodic)"
            )

    # -- the four verbs ------------------------------------------------------
    def store_value(self, proc: Any, key: str, value: Any, kind: str = "working") -> None:
        self._check_kind(kind)
        if not isinstance(key, str) or not key.strip():
            raise MemoryError_("memory keys must be non-empty strings")
        if kind == "episodic":
            raise MemoryError_("episodic memory is written by the kernel, not agents")

        vector = None
        if kind == "semantic":
            if not isinstance(value, str):
                raise MemoryError_("semantic memory stores text (str) values")
            vector = json.dumps(_embed(value))

        if kind == "shared":
            existing = self._shared_row(key)
            if existing is not None and existing["created_by"] != proc.name:
                raise MemoryError_(
                    f"shared key {key!r} belongs to {existing['created_by']}"
                )
            self.db.execute(
                "INSERT OR REPLACE INTO memory VALUES ('shared', '*', ?, ?, NULL, ?, ?, ?)",
                (
                    key,
                    json.dumps(value),
                    existing["shared_with"] if existing else json.dumps(["*"]),
                    proc.name,
                    time.time(),
                ),
            )
            return

        self.db.execute(
            "INSERT OR REPLACE INTO memory VALUES (?, ?, ?, ?, ?, NULL, ?, ?)",
            (kind, self._owner(proc, kind), key, json.dumps(value), vector,
             proc.name, time.time()),
        )

    def retrieve(
        self,
        proc: Any,
        key: str | None = None,
        kind: str = "working",
        query: str | None = None,
        top: int = 3,
        limit: int = 20,
    ) -> Any:
        if kind == "episodic":
            return [
                {"ts": e["ts"], "kind": e["kind"], "message": e["message"]}
                for e in self.store.logs(pid=proc.pid, limit=limit)
            ]
        self._check_kind(kind)

        if kind == "semantic" and query is not None:
            return self._semantic_search(proc, query, top)

        if kind == "shared":
            if key is None:
                return {
                    r["key"]: json.loads(r["value"])
                    for r in self.db.execute(
                        "SELECT * FROM memory WHERE mtype = 'shared'"
                    ).fetchall()
                    if self._may_read_shared(proc, r)
                }
            row = self._shared_row(key)
            if row is None or not self._may_read_shared(proc, row):
                return None  # absent and forbidden look identical, by design
            return json.loads(row["value"])

        owner = self._owner(proc, kind)
        if key is None:
            rows = self.db.execute(
                "SELECT key, value FROM memory WHERE mtype = ? AND owner = ?",
                (kind, owner),
            ).fetchall()
            return {r["key"]: json.loads(r["value"]) for r in rows}
        row = self.db.execute(
            "SELECT value FROM memory WHERE mtype = ? AND owner = ? AND key = ?",
            (kind, owner, key),
        ).fetchone()
        return json.loads(row["value"]) if row else None

    def share(self, proc: Any, key: str, with_agent: Any = "*") -> None:
        """Grant access: promote one of your working keys into shared memory,
        or widen the access list of a shared key you created."""
        target = str(with_agent)
        existing = self._shared_row(key)
        if existing is not None:
            if existing["created_by"] != proc.name:
                raise MemoryError_(f"shared key {key!r} belongs to {existing['created_by']}")
            allowed = set(json.loads(existing["shared_with"] or "[]"))
            allowed.add(target)
            self.db.execute(
                "UPDATE memory SET shared_with = ?, updated_at = ?"
                " WHERE mtype = 'shared' AND owner = '*' AND key = ?",
                (json.dumps(sorted(allowed)), time.time(), key),
            )
            return

        row = self.db.execute(
            "SELECT value FROM memory WHERE mtype = 'working' AND owner = ? AND key = ?",
            (str(proc.pid), key),
        ).fetchone()
        if row is None:
            raise MemoryError_(
                f"nothing to share: {key!r} is not in your working memory "
                "and is not a shared key you created"
            )
        self.db.execute(
            "INSERT OR REPLACE INTO memory VALUES ('shared', '*', ?, ?, NULL, ?, ?, ?)",
            (key, row["value"], json.dumps([target]), proc.name, time.time()),
        )

    def delete(self, proc: Any, key: str, kind: str = "working") -> bool:
        self._check_kind(kind)
        if kind == "shared":
            existing = self._shared_row(key)
            if existing is None:
                return False
            if existing["created_by"] != proc.name:
                raise MemoryError_(f"shared key {key!r} belongs to {existing['created_by']}")
            self.db.execute(
                "DELETE FROM memory WHERE mtype = 'shared' AND owner = '*' AND key = ?",
                (key,),
            )
            return True
        cur = self.db.execute(
            "DELETE FROM memory WHERE mtype = ? AND owner = ? AND key = ?",
            (kind, self._owner(proc, kind), key),
        )
        return cur.rowcount > 0

    # -- kernel hooks --------------------------------------------------------
    def forget_process(self, pid: int) -> None:
        """A process exited: its private memory is freed, like any OS would."""
        self.db.execute(
            "DELETE FROM memory WHERE mtype IN ('working', 'scratchpad') AND owner = ?",
            (str(pid),),
        )

    # -- internals -----------------------------------------------------------
    def _shared_row(self, key: str):
        return self.db.execute(
            "SELECT * FROM memory WHERE mtype = 'shared' AND owner = '*' AND key = ?",
            (key,),
        ).fetchone()

    def _may_read_shared(self, proc: Any, row: Any) -> bool:
        if row["created_by"] == proc.name:
            return True
        allowed = set(json.loads(row["shared_with"] or "[]"))
        return "*" in allowed or str(proc.pid) in allowed or proc.name in allowed

    def _semantic_search(self, proc: Any, query: str, top: int) -> list[dict[str, Any]]:
        needle = _embed(query)
        rows = self.db.execute(
            "SELECT key, value, vector FROM memory WHERE mtype = 'semantic' AND owner = ?",
            (proc.name,),
        ).fetchall()
        scored = [
            {
                "key": r["key"],
                "text": json.loads(r["value"]),
                "score": round(_similarity(needle, json.loads(r["vector"])), 4),
            }
            for r in rows
            if r["vector"]
        ]
        scored.sort(key=lambda s: -s["score"])
        return scored[: max(1, top)]
