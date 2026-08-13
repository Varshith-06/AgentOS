"""Temporal workflow + activities for compare.py."""

from __future__ import annotations

import os
import sqlite3
import time
from datetime import timedelta

from temporalio import activity, workflow

TASK_QUEUE = "agentos-compare"


def _tally(i: int) -> None:
    conn = sqlite3.connect(os.environ["BENCH_TALLY"], isolation_level=None)
    conn.execute("INSERT INTO calls VALUES (?, ?)", (i, time.time()))
    conn.close()


@activity.defn
async def billable(i: int) -> int:
    """One billable call: the irreversible write, then the work."""
    _tally(i)
    time.sleep(float(os.environ.get("BENCH_DELAY", "0.08")))
    return i


@activity.defn
async def durable_step(i: int) -> int:
    """A durable step. BENCH_DELAY=0 measures framework overhead on its own."""
    _tally(i)
    delay = float(os.environ.get("BENCH_DELAY", "0"))
    if delay:
        time.sleep(delay)
    return i


@workflow.defn
class BillWorkflow:
    @workflow.run
    async def run(self, calls: int) -> int:
        for i in range(calls):
            # Sized for an 80ms activity: a 30s timeout would hide a dead worker for 30s.
            await workflow.execute_activity(
                billable, i, start_to_close_timeout=timedelta(seconds=2)
            )
        return calls


@workflow.defn
class StepWorkflow:
    @workflow.run
    async def run(self, steps: int) -> int:
        for i in range(steps):
            await workflow.execute_activity(
                durable_step, i, start_to_close_timeout=timedelta(seconds=10)
            )
        return steps


@workflow.defn
class GatedWorkflow:
    """The human-in-the-loop analog: block until a signal arrives."""

    def __init__(self) -> None:
        self._approved = False

    @workflow.signal
    def approve(self) -> None:
        self._approved = True

    @workflow.run
    async def run(self) -> str:
        await workflow.wait_condition(lambda: self._approved)
        return "resumed"
