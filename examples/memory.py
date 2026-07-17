"""The memory manager (AgentOS.pdf p.6).

Six kinds of memory behind one API — store / retrieve / share / delete — and
the backend is invisible to the agents.

The Researcher keeps a private working draft, shares one finding into shared
memory (which publishes MemoryUpdated), files facts into semantic memory, and
bumps a longterm counter. The Writer proves the boundaries: it is woken by the
event, it CAN read the shared finding, it CANNOT read the Researcher's working
draft, and it never touches the Researcher directly.

Run it twice:   python -m agentos.cli run examples/memory.py
                python -m agentos.cli run examples/memory.py
The longterm counter climbs across runs — that memory belongs to the agent's
name, not to a pid, and pids do not survive a restart.
"""

from __future__ import annotations

import asyncio

from agentos import Agent, Kernel


class Researcher(Agent):
    async def run(self, ctx):
        # Working memory: private to this process, freed when it exits.
        await ctx.memory.store("draft", {"hunch": "agents should be processes"})

        # Semantic memory: text plus a vector, retrieved by similarity.
        facts = [
            "the scheduler hands execution slots to ready agents",
            "the event bus wakes subscribers the publisher never names",
            "a deadlock is refused the moment the wait would close a cycle",
        ]
        for i, fact in enumerate(facts):
            await ctx.memory.store(f"fact-{i}", fact, kind="semantic")

        hits = await ctx.memory.retrieve(
            kind="semantic", query="who decides which agent runs next", top=1
        )
        await ctx.log(f"semantic search says: {hits[0]['text']!r}")

        # Longterm memory: keyed by agent name, survives restarts.
        runs = (await ctx.memory.retrieve("runs", kind="longterm")) or 0
        await ctx.memory.store("runs", runs + 1, kind="longterm")
        await ctx.log(f"this example has now run {runs + 1} time(s)")

        await ctx.sleep(0.05)  # let the Writer subscribe first
        # Shared memory: the only way agents pass state — through the kernel.
        await ctx.memory.store(
            "finding", f"run #{runs + 1}: {hits[0]['text']}", kind="shared"
        )
        return {"runs": runs + 1}


class Writer(Agent):
    async def run(self, ctx):
        await ctx.subscribe("MemoryUpdated")
        event = await ctx.wait_event("MemoryUpdated")

        spied = await ctx.memory.retrieve("draft")  # Researcher's working memory?
        shared = await ctx.memory.retrieve(event["key"], kind="shared")
        history = await ctx.memory.retrieve(kind="episodic", limit=10)

        await ctx.log(f"shared finding from {event['by']}: {shared!r}")
        return {
            "shared": shared,
            "spied_on_working_memory": spied,  # None: private means private
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
