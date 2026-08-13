"""The research assistant: memory doing the coordinating.

    python -m agentos.cli run examples/research_assistant.py

Neither Searcher talks to anyone; they file findings into shared memory and
the Synthesizer reads all of it without naming a key. Kill one mid-run and
the rest completes with what remains.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from agentos import Agent, Kernel
from agentos.kernel.models import DEFAULT_MODELS_CONFIG

MODELS = Path(".agentos/models.json")


class Searcher(Agent):
    async def run(self, ctx):
        aspect = self.params["aspect"]
        reply = await ctx.request_model(
            "fast", prompt=f"One concrete fact about {aspect}, one sentence."
        )
        fact = reply["text"]
        await ctx.memory.store(f"note-{aspect}", fact, kind="longterm")
        await ctx.memory.store(f"finding-{aspect}", fact, kind="shared")
        await ctx.log(f"filed a finding on {aspect}")
        return {"aspect": aspect}


class Synthesizer(Agent):
    async def run(self, ctx):
        await ctx.wait_all(agents=self.params["searchers"])
        findings = await ctx.memory.retrieve(kind="shared")
        draft = await ctx.request_model(
            "fast",
            prompt="Synthesize a two-sentence brief from: "
            + " | ".join(str(v) for v in findings.values()),
        )
        await ctx.memory.store("draft", draft["text"], kind="shared")
        return {"sources": len(findings)}


class Critic(Agent):
    async def run(self, ctx):
        await ctx.wait(self.params["synthesizer"])
        draft = await ctx.memory.retrieve("draft", kind="shared")
        verdict = await ctx.request_model(
            "fast", prompt=f"One-line critique of this brief: {draft!r}"
        )
        return {"verdict": verdict["text"][:100]}


class Planner(Agent):
    priority = "High"

    async def run(self, ctx):
        topic = self.params["topic"]
        searchers = [
            await ctx.spawn(Searcher(aspect=f"{topic} ({aspect})"))
            for aspect in ("performance", "cost")
        ]
        synth = await ctx.spawn(Synthesizer(searchers=searchers))
        critic = await ctx.spawn(Critic(synthesizer=synth))
        result = await ctx.wait_all(agents=[synth, critic])
        return {
            "topic": topic,
            "synthesis": result["agents"][synth],
            "critique": result["agents"][critic],
        }


async def main(slots: int = 4, policy: str = "dependency") -> int:
    if not MODELS.exists():
        MODELS.parent.mkdir(parents=True, exist_ok=True)
        MODELS.write_text(json.dumps(DEFAULT_MODELS_CONFIG, indent=2), encoding="utf-8")

    kernel = Kernel(policy=policy, slots=slots)
    result = await kernel.run_until_done(Planner(topic="vector databases"))
    print(f"\nreport: {result}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
