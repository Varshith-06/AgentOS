"""Externally visible runtime state."""

from __future__ import annotations

import json
import os
import sqlite3
import struct
import time
from pathlib import Path
from typing import Any

try:  # optional: turns the KNN scan into an index seek, nothing else
    import sqlite_vec
except ImportError:  # pragma: no cover - exercised by the stdlib-only CI
    sqlite_vec = None

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
    vector      BLOB,
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
-- Written before a side-effecting call is dispatched and settled in the same
-- transaction as its journal entry, so an unsettled row after a crash is a call
-- whose outcome nobody recorded: it may or may not have happened.
CREATE TABLE IF NOT EXISTS intents (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      REAL    NOT NULL,
    pid     INTEGER NOT NULL,
    req_id  INTEGER NOT NULL,
    op      TEXT    NOT NULL,
    target  TEXT    NOT NULL,
    key     TEXT    NOT NULL,
    settled INTEGER NOT NULL DEFAULT 0
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
CREATE TABLE IF NOT EXISTS tool_calls (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         REAL    NOT NULL,
    pid        INTEGER,
    agent      TEXT,
    capability TEXT    NOT NULL,
    op         TEXT    NOT NULL,
    latency    REAL    NOT NULL DEFAULT 0,
    ok         INTEGER NOT NULL DEFAULT 1,
    error      TEXT
);
-- logs(pid=...) backs episodic memory: without this it walks the whole table
-- backwards to find one dead agent's rows.
CREATE INDEX IF NOT EXISTS log_by_pid ON log (pid, id);
-- take_commands() runs every kernel tick; consumed rows stay until the next
-- runtime start, so the scan grows with the run. Partial: only the live ones.
CREATE INDEX IF NOT EXISTS commands_pending ON commands (id) WHERE consumed = 0;
CREATE INDEX IF NOT EXISTS approvals_by_pid ON approvals (pid, status);
"""

# Episodic memory is a ring buffer, not an archive. Trimmed every _TRIM_EVERY
# inserts so the cost amortises to nothing.
LOG_LIMIT = 50_000
_TRIM_EVERY = 1_000

VECTOR_DIM = 128
_VECTOR = "<%df"

# vec0 cannot live in SCHEMA: without the extension the whole script would fail.
VEC_SCHEMA = f"""
CREATE VIRTUAL TABLE IF NOT EXISTS memory_vec USING vec0(
    owner     TEXT partition key,
    mkey      TEXT,
    embedding float[{VECTOR_DIM}] distance_metric=cosine
);
"""


def pack_vector(values: list[float]) -> bytes:
    """Little-endian float32, the on-disk form of an embedding."""
    return struct.pack(_VECTOR % len(values), *values)


def unpack_vector(blob: bytes) -> tuple[float, ...]:
    return struct.unpack(_VECTOR % (len(blob) // 4), blob)


class Store:
    """Externally visible runtime state, in one SQLite file.

    The kernel holds the only writing connection; the CLI and the API thread
    each open their own and read. WAL serialises one writer against many
    readers, so a second writer would meet the 5s timeout as SQLITE_BUSY rather
    than queue behind the first: writes belong behind a syscall.

    `vec` reports whether sqlite-vec loaded. It is an accelerator for longterm
    search and never a source of truth: the `memory` table alone is, and the
    index is rebuilt from it at every runtime start.
    """

    def __init__(self, dirpath: Path | str = DEFAULT_DIR) -> None:
        self.dir = Path(dirpath)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / "runtime.db"
        self.db = sqlite3.connect(self.path, isolation_level=None, timeout=5.0)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")  # concurrent CLI reads
        # WAL + NORMAL fsyncs at checkpoints rather than per commit: a killed
        # process still cannot lose committed data, and 400 writes save ~273ms.
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(SCHEMA)
        self.vec = self._load_vec()

    def _load_vec(self) -> bool:
        if sqlite_vec is None:
            return False
        try:
            self.db.enable_load_extension(True)
            sqlite_vec.load(self.db)
            self.db.executescript(VEC_SCHEMA)
        except (AttributeError, sqlite3.Error):
            return False  # built without extension support, or no wheel here
        finally:
            try:
                self.db.enable_load_extension(False)
            except AttributeError:
                pass
        return True

    def register_runtime(self, policy: str, slots: int) -> None:
        now = time.time()
        self.db.execute("DELETE FROM processes")
        self.db.execute("DELETE FROM commands")
        self.db.execute("DELETE FROM events")
        self.db.execute("DELETE FROM log")
        self.db.execute("DELETE FROM journal")
        self.db.execute("DELETE FROM intents")
        self.db.execute("DELETE FROM consumptions")
        self.db.execute("UPDATE approvals SET pid = NULL WHERE status = 'pending'")
        self.db.execute("DELETE FROM memory WHERE mtype IN ('working', 'shared')")
        self.db.execute("DELETE FROM model_calls")
        self.db.execute("DELETE FROM tool_calls")
        self.db.execute(
            "INSERT OR REPLACE INTO runtime VALUES (1, ?, ?, ?, ?, ?)",
            (os.getpid(), policy, slots, now, now),
        )
        self.sync_vectors()

    def resume_runtime(self, policy: str, slots: int) -> None:
        """Take over after a crash. Nothing is wiped: all of it is what recovery needs."""
        now = time.time()
        self.db.execute("DELETE FROM commands")
        self.db.execute(
            "INSERT OR REPLACE INTO runtime VALUES (1, ?, ?, ?, ?, ?)",
            (os.getpid(), policy, slots, now, now),
        )
        self.sync_vectors()

    def sync_vectors(self) -> None:
        """Migrate JSON-text embeddings to float32, then rebuild the KNN index."""
        for r in self.db.execute(
            "SELECT owner, key, vector FROM memory"
            " WHERE mtype = 'longterm' AND typeof(vector) = 'text'"
        ).fetchall():
            self.db.execute(
                "UPDATE memory SET vector = ?"
                " WHERE mtype = 'longterm' AND owner = ? AND key = ?",
                (pack_vector(json.loads(r["vector"])), r["owner"], r["key"]),
            )
        if not self.vec:
            return
        # A run without the extension leaves the index stale, so never trust it:
        # longterm rows are few and this is once per start.
        self.db.execute("DELETE FROM memory_vec")
        self.db.executemany(
            "INSERT INTO memory_vec (owner, mkey, embedding) VALUES (?, ?, ?)",
            self.db.execute(
                "SELECT owner, key, vector FROM memory"
                " WHERE mtype = 'longterm' AND LENGTH(vector) = ?",
                (VECTOR_DIM * 4,),  # vec0 rejects the row; the table is the truth
            ).fetchall(),
        )

    def index_vector(self, owner: str, key: str, blob: bytes | None) -> None:
        """Mirror one longterm row into the KNN index. A None vector unindexes."""
        if not self.vec:
            return
        self.db.execute(
            "DELETE FROM memory_vec WHERE owner = ? AND mkey = ?", (owner, key)
        )
        if blob is not None:
            self.db.execute(
                "INSERT INTO memory_vec (owner, mkey, embedding) VALUES (?, ?, ?)",
                (owner, key, blob),
            )

    def nearest(self, owner: str, needle: bytes, top: int) -> list[tuple[str, float]]:
        """(key, cosine similarity) for the closest `top` of one agent's memories."""
        rows = self.db.execute(
            "SELECT mkey, distance FROM memory_vec"
            " WHERE owner = ? AND embedding MATCH ? AND k = ?",
            (owner, needle, top),
        ).fetchall()
        return [(r["mkey"], 1.0 - r["distance"]) for r in rows]

    def heartbeat(self) -> None:
        self.db.execute("UPDATE runtime SET heartbeat = ? WHERE id = 1", (time.time(),))

    def flush(self) -> None:
        """Push the write-ahead log into the database file."""
        try:
            self.db.execute("PRAGMA wal_checkpoint(PASSIVE)")
        except sqlite3.OperationalError:
            pass  # another connection holds it; the next flush gets it

    def runtime_info(self) -> dict[str, Any] | None:
        row = self.db.execute("SELECT * FROM runtime WHERE id = 1").fetchone()
        return dict(row) if row else None

    def publish(self, row: dict[str, Any]) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO processes VALUES (?, ?, ?)",
            (row["pid"], json.dumps(row), time.time()),
        )

    def processes(self) -> list[dict[str, Any]]:
        rows = self.db.execute("SELECT row FROM processes ORDER BY pid").fetchall()
        return [json.loads(r["row"]) for r in rows]

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

    def append_log(self, pid: int | None, kind: str, message: str) -> None:
        cur = self.db.execute(
            "INSERT INTO log (ts, pid, kind, message) VALUES (?, ?, ?, ?)",
            (time.time(), pid, kind, message),
        )
        # AUTOINCREMENT ids never repeat, so this keeps the newest LOG_LIMIT.
        if cur.lastrowid % _TRIM_EVERY == 0:
            self.db.execute(
                "DELETE FROM log WHERE id <= ?", (cur.lastrowid - LOG_LIMIT,)
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

    def request_approval(
        self, agent: str, role: str, reason: str, pid: int
    ) -> dict[str, Any]:
        """Find or create the approval object this request refers to."""
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
        """Grant the pending approval for `pid`, validating the role."""
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

    def append_journal(
        self, pid: int, req_id: int, op: str, value: Any, error: str | None,
        intent: int | None = None,
    ) -> None:
        """Record a reply, settling the intent it answers in the same commit."""
        self.db.execute("BEGIN")
        try:
            self.db.execute(
                "INSERT INTO journal (ts, pid, req_id, op, value, error)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (time.time(), pid, req_id, op, json.dumps(value), error),
            )
            if intent is not None:
                self.db.execute("UPDATE intents SET settled = 1 WHERE id = ?", (intent,))
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def append_intent(
        self, pid: int, req_id: int, op: str, target: str, key: str
    ) -> int:
        cur = self.db.execute(
            "INSERT INTO intents (ts, pid, req_id, op, target, key)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (time.time(), pid, req_id, op, target, key),
        )
        return int(cur.lastrowid)

    def settle_intent(self, intent: int) -> None:
        self.db.execute("UPDATE intents SET settled = 1 WHERE id = ?", (intent,))

    def open_intents(self) -> dict[int, dict[str, Any]]:
        """pid -> the call it had in flight when the runtime died, if any."""
        rows = self.db.execute(
            "SELECT * FROM intents WHERE settled = 0 ORDER BY id"
        ).fetchall()
        return {r["pid"]: dict(r) for r in rows}

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

    def record_tool_call(
        self, pid: int, agent: str, capability: str, op: str,
        latency: float, ok: bool, error: str | None,
    ) -> None:
        self.db.execute(
            "INSERT INTO tool_calls (ts, pid, agent, capability, op, latency,"
            " ok, error) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (time.time(), pid, agent, capability, op, latency, int(ok), error),
        )

    def tool_usage(self) -> dict[str, dict[str, Any]]:
        """capability -> calls, failures, and mean latency this run."""
        rows = self.db.execute(
            "SELECT capability, COUNT(*) AS calls,"
            " SUM(CASE WHEN ok = 0 THEN 1 ELSE 0 END) AS failed,"
            " AVG(latency) AS mean_latency"
            " FROM tool_calls GROUP BY capability ORDER BY calls DESC"
        ).fetchall()
        return {
            r["capability"]: {
                "calls": r["calls"],
                "failed": int(r["failed"] or 0),
                "mean_latency": round(float(r["mean_latency"] or 0.0), 3),
            }
            for r in rows
        }

    def model_usage(self) -> dict[str, dict[str, Any]]:
        """model -> calls, tokens, cost, mean latency. model_costs() answers per agent."""
        rows = self.db.execute(
            "SELECT model, COUNT(*) AS calls, SUM(input_tokens) AS input_tokens,"
            " SUM(output_tokens) AS output_tokens, SUM(cost) AS cost,"
            " AVG(latency) AS mean_latency,"
            " SUM(CASE WHEN ok = 0 THEN 1 ELSE 0 END) AS failed"
            " FROM model_calls GROUP BY model ORDER BY calls DESC"
        ).fetchall()
        return {
            r["model"]: {
                "calls": r["calls"],
                "input_tokens": int(r["input_tokens"] or 0),
                "output_tokens": int(r["output_tokens"] or 0),
                "cost": float(r["cost"] or 0.0),
                "mean_latency": round(float(r["mean_latency"] or 0.0), 3),
                "failed": int(r["failed"] or 0),
            }
            for r in rows
        }

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
