"""How long a test waits before it calls something a deadlock."""

from __future__ import annotations

import os

# Seconds a wait may take before the test treats it as a deadlock.
LIMIT = float(os.environ.get("AGENTOS_TEST_TIMEOUT", "60"))

# How long a publisher waits for its subscribers to reach their first
# syscall. An agent is an OS process, so that costs a process start:
# ~100ms idle and more when several start at once. 50ms was below the floor.
STARTUP = float(os.environ.get("AGENTOS_TEST_STARTUP", "2.0"))
