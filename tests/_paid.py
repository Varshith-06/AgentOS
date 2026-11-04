"""A paid call a courier can make: importable, and nothing but its arguments."""

from __future__ import annotations

import os
import sqlite3
import time


GATE_LIMIT = 120.0  # no test holds a call this long


def bill(n: int, db: str, gate: str | None = None, latency: float = 0.0,
         idempotency_key: str | None = None) -> dict:
    """Charge on receipt, like a provider; answer once `gate` exists, if given."""
    conn = sqlite3.connect(db, isolation_level=None, timeout=30)
    try:
        conn.execute("INSERT INTO charges VALUES (?)", (n,))
    finally:
        conn.close()
    deadline = time.monotonic() + GATE_LIMIT
    while gate is not None and not os.path.exists(gate):
        # A test's teardown opens the gate and removes its folder at once, so a
        # poll can miss the gate altogether. Without this the call never returns,
        # and the interpreter waits for its thread at exit.
        if not os.path.isdir(os.path.dirname(gate)) or time.monotonic() > deadline:
            break
        time.sleep(0.01)
    time.sleep(latency)
    return {"charged": n}
