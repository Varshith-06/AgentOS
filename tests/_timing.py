"""How long a test waits before it calls something a deadlock.

Every `asyncio.wait_for` in this suite is a backstop, not an assertion. The
tests check *what* happened — a state, a result, a ledger exact to the token —
and never how fast it happened. The timeout is only there so that a genuine
deadlock fails the run instead of hanging it forever.

That makes the number a trade: how quickly a deadlock gets reported, against
how much slower some other machine may be than the one the test was written
on. Five seconds was comfortable on a 16-core desktop and far too tight on a
loaded four-core CI runner, where every one of these waits is queued behind
real agent processes competing for real cores. Under deliberate CPU
contention the old value failed 8 of the 13 files, and not one of them had
deadlocked — they were merely slow. A false failure costs more than a slow
report, so the backstop is generous.

Override it on a very slow box:

    AGENTOS_TEST_TIMEOUT=180 python tests/run.py
"""

from __future__ import annotations

import os

# Seconds a wait may take before the test treats it as a deadlock.
LIMIT = float(os.environ.get("AGENTOS_TEST_TIMEOUT", "60"))

# How long a publisher waits for its subscribers to reach their first
# syscall. An agent is an OS process, so that costs a process start:
# ~100ms idle and more when several start at once. 50ms was below the floor.
STARTUP = float(os.environ.get("AGENTOS_TEST_STARTUP", "2.0"))
