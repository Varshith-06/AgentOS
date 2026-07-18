"""The SQL driver: SQLite behind the capability, rows out as plain data."""

from __future__ import annotations

import sqlite3
from typing import Any

from .base import ToolDriver


class SQL(ToolDriver):
    name = "sql"
    timeout = 15.0
    cacheable = ("query",)  # reads only; caching a write would be a bug

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

    async def op_execute(self, statement: str, params: list | None = None) -> dict:
        cur = self._connect().execute(statement, params or [])
        return {"rowcount": cur.rowcount}
