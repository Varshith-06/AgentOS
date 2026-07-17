"""Externally visible runtime state.

`agent ps` runs in a different terminal than the runtime, so the process table
has to live somewhere both can see. Phase 1 uses SQLite at .agentos/runtime.db:
the kernel publishes the process table on every transition, and the CLI reads
it. Control commands (kill/pause/resume) go the other way through a queue that
the kernel polls.

This is deliberately the same shape as the Phase 7 daemon — a control plane the
kernel serves and clients talk to — so that swapping SQLite for the FastAPI
daemon later is a transport change, not a redesign.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

DEFAULT_DIR = Path(".agentos")

SCHEMA = """
CREATE TABLE IF NOT EXISTS runtime (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    pid_os      INTEGER NOT NULL,
    policy      TEXT    NOT NULL,
    slots       INTEGER NOT NULL,
    started_at  REAL    NOT NULL,
    heartbeat   REAL    NOT NULL
);
CREATE TABLE IF NOT EXISTS processes (
    pid         INTEGER PRIMARY KEY,
    row         TEXT    NOT NULL,
    updated_at  REAL    NOT NULL
);
CREATE TABLE IF NOT EXISTS commands (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    op          TEXT    NOT NULL,
    args        TEXT    NOT NULL,
    created_at  REAL    NOT NULL,
    consumed    INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL    NOT NULL,
    pid         INTEGER,
    kind        TEXT    NOT NULL,
    message     TEXT    NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    seq         INTEGER PRIMARY KEY,
    ts          REAL    NOT NULL,
    type        TEXT    NOT NULL,
    source_pid  INTEGER,
    payload     TEXT    NOT NULL,
    subscribers TEXT    NOT NULL
);
CREATE TABLE IF NOT EXISTS approvals (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    agent        TEXT    NOT NULL,
    role         TEXT    NOT NULL,
    reason       TEXT    NOT NULL,
    pid          INTEGER,
    status       TEXT    NOT NULL DEFAULT 'pending',
    requested_at REAL    NOT NULL,
    resolved_at  REAL,
    resolved_by  TEXT
);
CREATE TABLE IF NOT EXISTS memory (
    mtype       TEXT    NOT NULL,
    owner       TEXT    NOT NULL,
    key         TEXT    NOT NULL,
    value       TEXT    NOT NULL,
    vector      TEXT,
    shared_with TEXT,
    created_by  TEXT,
    updated_at  REAL    NOT NULL,
    PRIMARY KEY (mtype, owner, key)
);
CREATE TABLE IF NOT EXISTS journal (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    ts     REAL    NOT NULL,
    pid    INTEGER NOT NULL,
    req_id INTEGER NOT NULL,
    op     TEXT    NOT NULL,
    value  TEXT    NOT NULL,
    error  TEXT
);
CREATE TABLE IF NOT EXISTS consumptions (
    pid    INTEGER NOT NULL,
    seq    INTEGER NOT NULL,
    PRIMARY KEY (pid, seq)
);
CREATE TABLE IF NOT EXISTS model_calls (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            REAL    NOT NULL,
    pid           INTEGER,
    agent         TEXT,
    need          TEXT    NOT NULL,
    model         TEXT    NOT NULL,
    input_tokens  INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cost          REAL    NOT NULL DEFAULT 0,
    latency       REAL    NOT NULL DEFAULT 0,
    ok            INTEGER NOT NULL DEFAULT 1,
    error         TEXT
);
"""


class Store:
    def __init__(self, dirpath: Path | str = DEFAULT_DIR) -> None:
        self.dir = Path(dirpath)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / "runtime.db"
        self.db = sqlite3.connect(self.path, isolation_level=None, timeout=5.0)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")  # concurrent CLI reads
        self.db.executescript(SCHEMA)

    # -- runtime identity ------------------------------------------------
    def register_runtime(self, policy: str, slots: int) -> None:
        now = time.time()
        self.db.execute("DELETE FROM processes")
        self.db.execute("DELETE FROM commands")
        self.db.execute("DELETE FROM events")
        self.db.execute("DELETE FROM log")
        self.db.execute("DELETE FROM journal")
        self.db.execute("DELETE FROM consumptions")
        # Approvals deliberately survive a restart — a human dependency that
        # evaporates on restart is not a kernel object. Only the pid is
        # meaningless in the new runtime; a re-run agent re-attaches by identity.
        self.db.execute("UPDATE approvals SET pid = NULL WHERE status = 'pending'")
        # Ephemeral memory belongs to the run; longterm and semantic memory
        # are keyed by agent name and deliberately survive (p.6).
        self.db.execute(
            "DELETE FROM memory WHERE mtype IN ('working', 'scratchpad', 'shared')"
        )
        self.db.execute("DELETE FROM model_calls")
        self.db.execute(
            "INSERT OR REPLACE INTO runtime VALUES (1, ?, ?, ?, ?, ?)",
            (os.getpid(), policy, slots, now, now),
        )

    def resume_runtime(self, policy: str, slots: int) -> None:
        """Take over after a crash. Nothing is wiped: the process table, the
        journals, the events, and the memory are exactly what recovery needs."""
        now = time.time()
        self.db.execute("DELETE FROM commands")  # stale control commands only
        self.db.execute(
            "INSERT OR REPLACE INTO runtime VALUES (1, ?, ?, ?, ?, ?)",
            (os.getpid(), policy, slots, now, now),
        )

    def heartbeat(self) -> None:
        self.db.execute("UPDATE runtime SET heartbeat = ? WHERE id = 1", (time.time(),))

    def runtime_info(self) -> dict[str, Any] | None:
        row = self.db.execute("SELECT * FROM runtime WHERE id = 1").fetchone()
        return dict(row) if row else None

    # -- process table ---------------------------------------------------
    def publish(self, row: dict[str, Any]) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO processes VALUES (?, ?, ?)",
            (row["pid"], json.dumps(row), time.time()),
        )

    def processes(self) -> list[dict[str, Any]]:
        rows = self.db.execute("SELECT row FROM processes ORDER BY pid").fetchall()
        return [json.loads(r["row"]) for r in rows]

    # -- control commands (CLI -> kernel) --------------------------------
    def send_command(self, op: str, **args: Any) -> int:
        cur = self.db.execute(
            "INSERT INTO commands (op, args, created_at) VALUES (?, ?, ?)",
            (op, json.dumps(args), time.time()),
        )
        return int(cur.lastrowid)

    def take_commands(self) -> list[tuple[str, dict[str, Any]]]:
        rows = self.db.execute(
            "SELECT id, op, args FROM commands WHERE consumed = 0 ORDER BY id"
        ).fetchall()
        if not rows:
            return []
        self.db.execute(
            "UPDATE commands SET consumed = 1 WHERE id IN (%s)"
            % ",".join(str(r["id"]) for r in rows)
        )
        return [(r["op"], json.loads(r["args"])) for r in rows]

    # -- log -------------------------------------------------------------
    def append_log(self, pid: int | None, kind: str, message: str) -> None:
        self.db.execute(
            "INSERT INTO log (ts, pid, kind, message) VALUES (?, ?, ?, ?)",
            (time.time(), pid, kind, message),
        )

    def logs(self, pid: int | None = None, limit: int = 200) -> list[dict[str, Any]]:
        if pid is None:
            rows = self.db.execute(
                "SELECT * FROM log ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        else:
            rows = self.db.execute(
                "SELECT * FROM log WHERE pid = ? ORDER BY id DESC LIMIT ?", (pid, limit)
            ).fetchall()
        return [dict(r) for r in reversed(rows)]

    # -- human approvals (Phase 3, p.5-6) ----------------------------------
    def request_approval(
        self, agent: str, role: str, reason: str, pid: int
    ) -> dict[str, Any]:
        """Find or create the approval object this request refers to.

        Identity is (agent, role, reason). A grant issued while the runtime was
        down is honored here, and a pending request orphaned by a restart —
        pid nulled by a fresh boot, or still carrying this same pid after a
        crash recovery — is adopted instead of asking the human twice.
        """
        row = self.db.execute(
            "SELECT * FROM approvals WHERE agent = ? AND role = ? AND reason = ?"
            " AND (status = 'granted'"
            "      OR (status = 'pending' AND (pid IS NULL OR pid = ?)))"
            " ORDER BY id LIMIT 1",
            (agent, role, reason, pid),
        ).fetchone()
        if row is not None:
            self.db.execute(
                "UPDATE approvals SET pid = ? WHERE id = ?", (pid, row["id"])
            )
            return {**dict(row), "pid": pid}
        cur = self.db.execute(
            "INSERT INTO approvals (agent, role, reason, pid, status, requested_at)"
            " VALUES (?, ?, ?, ?, 'pending', ?)",
            (agent, role, reason, pid, time.time()),
        )
        return self.approval(int(cur.lastrowid))

    def approve(self, pid: int, role: str) -> dict[str, Any]:
        """Grant the pending approval for `pid`, validating the role.

        This writes the grant to the store directly rather than queueing a
        command: a grant is durable state, which is what lets a human approve
        while the runtime is down and have the restarted run honor it.
        """
        row = self.pending_approval_for(pid)
        if row is None:
            raise ValueError(f"pid {pid} has no pending approval")
        if row["role"] != role:
            raise ValueError(
                f"pid {pid} needs approval from {row['role']!r}; {role!r} cannot grant it"
            )
        self.db.execute(
            "UPDATE approvals SET status = 'granted', resolved_at = ?, resolved_by = ?"
            " WHERE id = ?",
            (time.time(), role, row["id"]),
        )
        return self.approval(row["id"])

    def consume_approval(self, approval_id: int) -> None:
        """The blocked agent has been woken: this grant is spent."""
        self.db.execute(
            "UPDATE approvals SET status = 'consumed' WHERE id = ?", (approval_id,)
        )

    def approval(self, approval_id: int) -> dict[str, Any] | None:
        row = self.db.execute(
            "SELECT * FROM approvals WHERE id = ?", (approval_id,)
        ).fetchone()
        return dict(row) if row else None

    def pending_approval_for(self, pid: int) -> dict[str, Any] | None:
        row = self.db.execute(
            "SELECT * FROM approvals WHERE pid = ? AND status = 'pending'"
            " ORDER BY id LIMIT 1",
            (pid,),
        ).fetchone()
        return dict(row) if row else None

    def granted_approvals(self) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT * FROM approvals WHERE status = 'granted' ORDER BY id"
        ).fetchall()
        return [dict(r) for r in rows]

    def approvals(self, include_consumed: bool = False) -> list[dict[str, Any]]:
        query = "SELECT * FROM approvals"
        if not include_consumed:
            query += " WHERE status IN ('pending', 'granted')"
        rows = self.db.execute(query + " ORDER BY id").fetchall()
        return [dict(r) for r in rows]

    # -- the journal (Phase 6): every completed syscall is a checkpoint ------
    def append_journal(
        self, pid: int, req_id: int, op: str, value: Any, error: str | None
    ) -> None:
        self.db.execute(
            "INSERT INTO journal (ts, pid, req_id, op, value, error)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (time.time(), pid, req_id, op, json.dumps(value), error),
        )

    def load_journals(self) -> dict[int, list[dict[str, Any]]]:
        """pid -> its syscall replies, in order. The replay script."""
        out: dict[int, list[dict[str, Any]]] = {}
        for r in self.db.execute("SELECT * FROM journal ORDER BY pid, id").fetchall():
            out.setdefault(r["pid"], []).append(
                {
                    "req_id": r["req_id"],
                    "op": r["op"],
                    "value": json.loads(r["value"]),
                    "error": r["error"],
                }
            )
        return out

    def record_consumption(self, pid: int, seq: int) -> None:
        """This subscriber took this event off its buffer — never redeliver."""
        self.db.execute(
            "INSERT OR IGNORE INTO consumptions VALUES (?, ?)", (pid, seq)
        )

    def consumptions(self) -> set[tuple[int, int]]:
        return {
            (r["pid"], r["seq"])
            for r in self.db.execute("SELECT * FROM consumptions").fetchall()
        }

    # -- memory + model accounting (Phase 5): what the CLI reads -----------
    def memory_usage(self) -> dict[str, int]:
        """owner -> bytes of stored values. The p.3 process card's MEM figure."""
        rows = self.db.execute(
            "SELECT owner, SUM(LENGTH(value)) AS bytes FROM memory GROUP BY owner"
        ).fetchall()
        return {r["owner"]: int(r["bytes"] or 0) for r in rows}

    def record_model_call(
        self,
        pid: int | None,
        agent: str | None,
        need: str,
        model: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cost: float = 0.0,
        latency: float = 0.0,
        ok: bool = True,
        error: str | None = None,
    ) -> None:
        self.db.execute(
            "INSERT INTO model_calls (ts, pid, agent, need, model, input_tokens,"
            " output_tokens, cost, latency, ok, error)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                time.time(), pid, agent, need, model,
                input_tokens, output_tokens, cost, latency, int(ok), error,
            ),
        )

    def model_costs(self) -> dict[int, dict[str, Any]]:
        """pid -> accumulated calls, tokens, and dollars this run."""
        rows = self.db.execute(
            "SELECT pid, COUNT(*) AS calls, SUM(input_tokens) AS input_tokens,"
            " SUM(output_tokens) AS output_tokens, SUM(cost) AS cost"
            " FROM model_calls GROUP BY pid"
        ).fetchall()
        return {
            r["pid"]: {
                "calls": r["calls"],
                "input_tokens": int(r["input_tokens"] or 0),
                "output_tokens": int(r["output_tokens"] or 0),
                "cost": float(r["cost"] or 0.0),
            }
            for r in rows
            if r["pid"] is not None
        }

    def model_calls(self, limit: int = 200) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT * FROM model_calls ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in reversed(rows)]

    # -- events ----------------------------------------------------------
    def append_event(
        self,
        seq: int,
        event_type: str,
        source_pid: int | None,
        payload: dict[str, Any],
        subscribers: list[int],
    ) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO events VALUES (?, ?, ?, ?, ?, ?)",
            (
                seq,
                time.time(),
                event_type,
                source_pid,
                json.dumps(payload),
                json.dumps(subscribers),
            ),
        )

    def events(self, limit: int = 200) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT * FROM events ORDER BY seq DESC LIMIT ?", (limit,)
        ).fetchall()
        out = []
        for r in reversed(rows):
            e = dict(r)
            e["payload"] = json.loads(e["payload"])
            e["subscribers"] = json.loads(e["subscribers"])
            out.append(e)
        return out

    def close(self) -> None:
        self.db.close()
