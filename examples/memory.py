"""The memory manager: four kinds behind store / retrieve / share / delete.

    python -m agentos.cli run examples/memory.py     # twice

The Writer is woken by MemoryUpdated, can read the shared finding, and
cannot read the Researcher's working draft. The longterm counter climbs
across runs: that memory belongs to the agent's name, not to a pid.
"""

from __future__ import annotations

import asyncio

from agentos import Agent, Kernel


class Researcher(Agent):
    async def run(self, ctx):
        await ctx.memory.store("draft", {"hunch": "agents should be processes"})

        facts = [
            "the scheduler hands execution slots to ready agents",
            "the event bus wakes subscribers the publisher never names",
            "a deadlock is refused the moment the wait would close a cycle",
        ]
        for i, fact in enumerate(facts):
            await ctx.memory.store(f"fact-{i}", fact, kind="longterm")

        hits = await ctx.memory.retrieve(
            kind="longterm", query="who decides which agent runs next", top=1
        )
        await ctx.log(f"search by meaning says: {hits[0]['text']!r}")

        runs = (await ctx.memory.retrieve("runs", kind="longterm")) or 0
        await ctx.memory.store("runs", runs + 1, kind="longterm")
        await ctx.log(f"this example has now run {runs + 1} time(s)")

        await ctx.sleep(0.05)
        await ctx.memory.store(
            "finding", f"run #{runs + 1}: {hits[0]['text']}", kind="shared"
        )
        return {"runs": runs + 1}


class Writer(Agent):
    async def run(self, ctx):
        await ctx.subscribe("MemoryUpdated")
        event = await ctx.wait_event("MemoryUpdated")

        spied = await ctx.memory.retrieve("draft")
        shared = await ctx.memory.retrieve(event["key"], kind="shared")
        history = await ctx.memory.retrieve(kind="episodic", limit=10)

        await ctx.log(f"shared finding from {event['by']}: {shared!r}")
        return {
            "shared": shared,
            "spied_on_working_memory": spied,
            "own_history_entries": len(history),
        }


async def main(slots: int = 4, policy: str = "fifo") -> int:
    kernel = Kernel(policy=policy, slots=slots)
    writer = kernel.spawn(Writer())
    kernel.spawn(Researcher())
    await kernel.run()

    result = kernel.table.get(writer).result
    print(f"\nWriter got the shared finding:      {result['shared']!r}")
    print(f"Writer spying on working memory:    {result['spied_on_working_memory']!r}")
    print(f"Writer's own episodic history:      {result['own_history_entries']} entries")
    print("\nrun it again: the longterm counter keeps climbing")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
