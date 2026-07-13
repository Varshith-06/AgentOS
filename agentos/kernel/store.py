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
