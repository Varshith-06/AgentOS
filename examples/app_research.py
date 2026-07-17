"""Application #1 — a thin client (Phase 7, p.8).

This file owns no runtime. It connects to the daemon, hands over an agent as
data, and waits. Everything the agent needs — scheduling, model routing, cost
accounting, journaling — belongs to the daemon, and is shared with every
other application connected to it.

    python -m agentos.cli daemon        # terminal 1, once
    python examples/app_research.py     # terminal 2 (this file)
    python examples/app_support.py      # terminal 3
    python -m agentos.cli ps            # both apps' agents, one table
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # repo-checkout runs

from agentos import Agent, RuntimeClient  # noqa: E402


class ResearchJob(Agent):
    priority = "High"

    async def run(self, ctx):
        topic = self.params["topic"]
        await ctx.log(f"researching {topic}")
        reply = await ctx.request_model("fast", prompt=f"One insight about {topic}.")
        await ctx.memory.store("insight", reply["text"], kind="shared")
        await ctx.sleep(3)  # linger so `agent ps` catches both apps live
        return {"topic": topic, "model": reply["model"], "cost": reply["cost"]}


def main() -> int:
    client = RuntimeClient()
    print(f"runtime: {client.health()['url']} (not ours - we just use it)")
    pid = client.submit(ResearchJob(topic="vector databases"))
    print(f"submitted ResearchJob as pid {pid}; run `agent ps` now")
    result = client.wait(pid)
    print(f"done: {result}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
