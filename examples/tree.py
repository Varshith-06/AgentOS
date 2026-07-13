"""The agent tree from AgentOS.pdf p.8, with no LLM anywhere in sight.

    Planner
    ├── Research A
    ├── Research B
    ├── Research C
    └── Documentation

Every agent here just sleeps. That is the point: the scheduler, the process
table, and the lifecycle are verifiable without spending a token or needing a
key. If the kernel is wrong, it is wrong deterministically.

Run it:      python -m agentos.cli run examples/tree.py --slots 2
Watch it:    python -m agentos.cli top          (in a second terminal)
Kill a kid:  python -m agentos.cli kill 3       (in a second terminal)
"""

from __future__ import annotations

import asyncio

from agentos import Agent, Kernel


class Research(Agent):
    """A leaf agent. Sleeps, reports, returns a result."""

    async def run(self, ctx):
        topic = self.params["topic"]
        await ctx.log(f"researching {topic}")
        # Long enough that you can actually reach a second terminal and kill it.
        await ctx.sleep(self.params.get("duration", 8))
        await ctx.log(f"done researching {topic}")
        return {"topic": topic, "findings": f"3 sources on {topic}"}


class Documentation(Agent):
    priority = "Low"

    async def run(self, ctx):
        await ctx.log("writing docs")
        await ctx.sleep(10)
        return {"pages": 7}


class Planner(Agent):
    """Spawns children, waits for all of them, aggregates results (p.8)."""

    priority = "High"

    async def run(self, ctx):
        await ctx.log("planning")
        kids = []
        for topic in ("market", "legal", "technical"):
            pid = await ctx.spawn(Research(topic=topic))
            kids.append(pid)
            await ctx.log(f"spawned Research({topic}) as pid {pid}")

        doc_pid = await ctx.spawn(Documentation())
        await ctx.log(f"spawned Documentation as pid {doc_pid}")

        results = []
        for pid in kids:
            result = await ctx.wait(pid)  # status becomes Waiting
            await ctx.log(f"pid {pid} returned: {result}")
            results.append(result)

        await ctx.wait(doc_pid)
        await ctx.log(f"aggregated {len(results)} research results")
        return {"research": results}


async def main(slots: int = 4, policy: str = "fifo") -> int:
    kernel = Kernel(policy=policy, slots=slots)
    result = await kernel.run_until_done(Planner())

    print("\nfinal process table:")
    for row in kernel.store.processes():
        print(
            f"  pid {row['pid']:<3} {row['name']:<14} {row['status']:<9} "
            f"{row['exit_reason'] or ''}"
        )
    print(f"\nPlanner returned: {result}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
