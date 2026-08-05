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

#: Seconds a wait may take before the test treats it as a deadlock.
LIMIT = float(os.environ.get("AGENTOS_TEST_TIMEOUT", "60"))

#: How long a publisher waits for its subscribers to reach their first syscall.
#:
#: `subscribe` buffers everything that fires after it returns, so a subscriber
#: only misses an event published before it got that far.
#:
#: Waiting for the subscriber to reach WAITING and only then spawning the
#: publisher would be exact, and does not work: a kernel whose every agent is
#: WAITING has nothing runnable, which is indistinguishable from a deadlock,
#: so `run()` returns before the publisher is ever spawned. (The same trick in
#: test_events works only because that subscriber is SLEEPING — a pending
#: timer keeps the kernel alive.) Nor can the subscriber announce readiness;
#: doing so means publishing an event, which races the same way one level up.
#:
#: So the publisher has to be spawned upfront and allow time, and the
#: allowance has to clear the real floor — an agent is an OS process, so
#: reaching its first syscall costs a process start, about 100ms idle and
#: considerably more when several start at once on a busy machine.
#:
#: The old allowance was 50ms, below that floor even when idle. It passed on a
#: fast desktop and failed reproducibly on a loaded four-core runner.
STARTUP = float(os.environ.get("AGENTOS_TEST_STARTUP", "2.0"))
