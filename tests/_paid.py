"""A paid call a courier can make: importable, and nothing but its arguments."""

from __future__ import annotations

import os
import sqlite3
import time


def bill(n: int, db: str, gate: str | None = None, latency: float = 0.0,
         idempotency_key: str | None = None) -> dict:
    """Charge on receipt, like a provider; answer once `gate` exists, if given."""
    conn = sqlite3.connect(db, isolation_level=None, timeout=30)
    try:
        conn.execute("INSERT INTO charges VALUES (?)", (n,))
    finally:
        conn.close()
    while gate is not None and not os.path.exists(gate):
        time.sleep(0.01)
    time.sleep(latency)
    return {"charged": n}
