"""Temporal workflow for the crash study: one activity per billable call."""

from __future__ import annotations

import asyncio
from datetime import timedelta

from temporalio import activity, workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from .client import ACTIVITY_TIMEOUT as TIMEOUT, charge, keyed, note, question, signalling, think


@activity.defn
async def billable(i: int) -> dict:
    info = activity.info()
    note(i, attempt=info.attempt)
    if signalling():
        from .client import note_fields
        note_fields(i, attempt=info.attempt)
    key = f"{info.workflow_id}:{info.activity_id}" if keyed() else None
    await asyncio.to_thread(think)
    return await asyncio.to_thread(charge, i, key)


@activity.defn
async def billable_arg(i: int, q: str) -> dict:
    """The prompt comes from the workflow, as an activity's input normally does."""
    await asyncio.to_thread(think)
    return await asyncio.to_thread(charge, i, None, q)


@workflow.defn
class CallsArg:
    @workflow.run
    async def run(self, n: int) -> list:
        out = []
        for i in range(n):
            asked = question(i)  # from a module outside the sandbox: code or config, not workflow state
            reply = await workflow.execute_activity(
                billable_arg, args=[i, asked], start_to_close_timeout=timedelta(seconds=TIMEOUT),
                retry_policy=RetryPolicy(initial_interval=timedelta(milliseconds=100)),
            )
            out.append([i, asked, reply])
        return out


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
