"""Application #1: a thin client that owns no runtime.

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
        await ctx.sleep(3)
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
