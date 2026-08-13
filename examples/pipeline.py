"""The event-driven pipeline, plus a wait on a dependency set.

    python -m agentos.cli run examples/pipeline.py
    python -m agentos.cli events -v      # the timeline
"""

from __future__ import annotations

import asyncio

from agentos import Agent, Kernel


class Research(Agent):
    """Does the work, announces the fact. Knows nothing about its consumers."""

    priority = "High"

    async def run(self, ctx):
        topic = self.params["topic"]
        await ctx.log(f"researching {topic}")
        await ctx.sleep(1)
        findings = f"3 sources on {topic}"
        await ctx.publish("ResearchCompleted", topic=topic, findings=findings)
        return findings


class CodeAgent(Agent):
    """Woken by the event. Never named by the publisher."""

    async def run(self, ctx):
        await ctx.subscribe("ResearchCompleted")
        await ctx.log("waiting for research")
        event = await ctx.wait_event("ResearchCompleted")
        await ctx.log(f"woken by ResearchCompleted({event['topic']}) - writing code")
        await ctx.sleep(1)
        return {"module": f"{event['topic']}.py"}


class DocumentationAgent(Agent):
    priority = "Low"

    async def run(self, ctx):
        await ctx.subscribe("ResearchCompleted")
        event = await ctx.wait_event("ResearchCompleted")
        await ctx.log(f"woken by ResearchCompleted - documenting {event['topic']}")
        await ctx.sleep(1)
        return {"pages": 4}


class Reviewer(Agent):
    """The fourth subscriber. Adding it required touching nothing else."""

    async def run(self, ctx):
        await ctx.subscribe("ResearchCompleted")
        event = await ctx.wait_event("ResearchCompleted")
        await ctx.log(f"reviewing findings: {event['findings']}")
        return {"verdict": "approved"}


class Planner(Agent):
    """Waits on a dependency *set*, not a sequence (p.5)."""

    priority = "High"

    async def run(self, ctx):
        code = await ctx.spawn(CodeAgent())
        docs = await ctx.spawn(DocumentationAgent())
        review = await ctx.spawn(Reviewer())
        await ctx.spawn(Research(topic="vector-databases"))

        await ctx.log(f"waiting on pids {code}, {docs}, {review} + a 0.5s settle timer")
        result = await ctx.wait_all(agents=[code, docs, review], timer=0.5)

        await ctx.log(f"all dependencies satisfied: {result['agents']}")
        return result["agents"]


async def main(slots: int = 4, policy: str = "fifo") -> int:
    kernel = Kernel(policy=policy, slots=slots)
    result = await kernel.run_until_done(Planner())

    print("\nevent timeline:")
    for e in kernel.store.events():
        subs = ", ".join(f"pid {p}" for p in e["subscribers"]) or "nobody"
        src = "kernel" if e["source_pid"] is None else f"pid {e['source_pid']}"
        print(f"  {e['type']:<18} from {src:<8} -> woke {subs}")

    print(f"\nPlanner's dependencies resolved to: {result}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
