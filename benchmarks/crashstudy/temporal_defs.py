"""Temporal workflow for the crash study: one activity per billable call."""

from __future__ import annotations

import asyncio
from datetime import timedelta

from temporalio import activity, workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from .client import ACTIVITY_TIMEOUT as TIMEOUT, charge, keyed, note, think


@activity.defn
async def billable(i: int) -> dict:
    info = activity.info()
    note(i, attempt=info.attempt)
    key = f"{info.workflow_id}:{info.activity_id}" if keyed() else None
    await asyncio.to_thread(think)
    return await asyncio.to_thread(charge, i, key)


@workflow.defn
class Calls:
    @workflow.run
    async def run(self, n: int) -> str:
        for i in range(n):
            # A dead worker is noticed when this timeout lapses; sized to the
            # call, as Temporal's guidance says, so recovery is not all waiting.
            # Real completions have a long tail, so those runs set it higher.
            await workflow.execute_activity(
                billable, i, start_to_close_timeout=timedelta(seconds=TIMEOUT),
                retry_policy=RetryPolicy(initial_interval=timedelta(milliseconds=100)),
            )
        return "done"
