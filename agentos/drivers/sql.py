"""The SQL driver: SQLite behind the capability, rows out as plain data."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from .base import ToolDriver


class SQL(ToolDriver):
    name = "sql"
    timeout = 15.0
    cacheable = ("query",)
    idempotent = ("execute",)

    def __init__(self, db: str = ":memory:", **kw: Any) -> None:
        super().__init__(**kw)
        self.db = str(db)
        self._conn: sqlite3.Connection | None = None

    def _connect(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(self.db, isolation_level=None)
            self._conn.row_factory = sqlite3.Row
        return self._conn

    async def op_query(self, query: str, params: list | None = None) -> list[dict]:
        rows = self._connect().execute(query, params or []).fetchall()
        return [dict(r) for r in rows]

    async def op_execute(
        self, statement: str, params: list | None = None,
        idempotency_key: str | None = None,
    ) -> dict:
        conn = self._connect()
        if idempotency_key is None:
            cur = conn.execute(statement, params or [])
            return {"rowcount": cur.rowcount}
        # The effect and the record that it happened commit together, in the
        # database the effect lands in: a key is either fully applied or not at all.
        conn.execute(
            "CREATE TABLE IF NOT EXISTS _agentos_effects"
            " (key TEXT PRIMARY KEY, result TEXT NOT NULL)"
        )
        conn.execute("BEGIN IMMEDIATE")
        try:
            seen = conn.execute(
                "SELECT result FROM _agentos_effects WHERE key = ?", (idempotency_key,)
            ).fetchone()
            if seen is not None:
                conn.execute("COMMIT")
                return {**json.loads(seen[0]), "deduplicated": True}
            cur = conn.execute(statement, params or [])
            result = {"rowcount": cur.rowcount}
            conn.execute(
                "INSERT INTO _agentos_effects VALUES (?, ?)",
                (idempotency_key, json.dumps(result)),
            )
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        return result
