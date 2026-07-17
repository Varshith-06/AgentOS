"""The software company (Phase 8, p.10) — every kernel service in one pipeline.

A ProductManager spawns the team. Research announces its findings as an event;
Coder and DocWriter wake on it (nobody names anybody). Coder writes actual
files through the sandboxed filesystem driver. Reviewer waits on both, then
blocks on a human — a Release Manager must approve the ship. Every LLM call is
routed by capability class, so this runs offline on the mock provider and on a
frontier model if ANTHROPIC_API_KEY is set, with zero code changes.

The Release Manager is played by a coroutine so the demo is self-contained;
in real life it is a person in another terminal:

    python -m agentos.cli approvals
    python -m agentos.cli approve <pid> --as "Release Manager"

Run it:   python -m agentos.cli run examples/software_company.py
Watch:    python -m agentos.cli top          (second terminal)
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from agentos import Agent, Kernel
from agentos.kernel.models import DEFAULT_MODELS_CONFIG

MODELS = Path(".agentos/models.json")
WORKSPACE = ".agentos/company"


class Research(Agent):
    priority = "High"

    async def run(self, ctx):
        product = self.params["product"]
        reply = await ctx.request_model(
            "fast", prompt=f"One key requirement for a {product} tool, one sentence."
        )
        await ctx.publish("ResearchCompleted", product=product, finding=reply["text"])
        return {"finding": reply["text"][:80]}


class Coder(Agent):
    async def run(self, ctx):
        await ctx.subscribe("ResearchCompleted")
        event = await ctx.wait_event("ResearchCompleted")
        code = await ctx.request_model(
            "fast", prompt=f"Sketch a module plan for: {event['finding']}"
        )
        await ctx.request_tool(
            "filesystem", "write", path="main.py",
            content=f"# {event['product']}\n# {code['text'][:200]}\n",
        )
        await ctx.log("code written to the workspace")
        return {"files": ["main.py"]}


class DocWriter(Agent):
    priority = "Low"

    async def run(self, ctx):
        await ctx.subscribe("ResearchCompleted")
        event = await ctx.wait_event("ResearchCompleted")
        docs = await ctx.request_model(
            "fast", prompt=f"A one-line README tagline for {event['product']}."
        )
        return {"readme": docs["text"][:80]}


class Reviewer(Agent):
    async def run(self, ctx):
        coder, docs = self.params["coder"], self.params["docs"]
        result = await ctx.wait_all(agents=[coder, docs])
        await ctx.log(f"reviewed {result['agents'][coder]['files']}")
        approval = await ctx.request_approval(
            role="Release Manager", reason="ship v1.0"
        )  # state: Blocked, until the human decides
        return {"shipped": True, "approved_by": approval["by"]}


class ProductManager(Agent):
    priority = "High"

    async def run(self, ctx):
        await ctx.spawn(Research(product=self.params["product"]))
        coder = await ctx.spawn(Coder())
        docs = await ctx.spawn(DocWriter())
        reviewer = await ctx.spawn(Reviewer(coder=coder, docs=docs))
        result = await ctx.wait(reviewer)
        return {"product": self.params["product"], **result}


async def main(slots: int = 4, policy: str = "fifo") -> int:
    if not MODELS.exists():
        MODELS.parent.mkdir(parents=True, exist_ok=True)
        MODELS.write_text(json.dumps(DEFAULT_MODELS_CONFIG, indent=2), encoding="utf-8")

    kernel = Kernel(
        policy=policy, slots=slots,
        permissions={"Coder": ["filesystem"]},
        tools={"filesystem": {"root": WORKSPACE}},
    )
    kernel.spawn(ProductManager(product="todo-cli"))

    async def release_manager():
        """The human, played by a coroutine so the demo is self-contained."""
        while True:
            await asyncio.sleep(0.2)
            for row in kernel.store.approvals():
                if row["status"] == "pending" and row["pid"]:
                    print(f"[release manager] approving pid {row['pid']}: {row['reason']}")
                    kernel.approve(row["pid"], row["role"])
                    return

    approver = asyncio.create_task(release_manager())
    await kernel.run()
    approver.cancel()

    pm = kernel.table.get(1)
    print(f"\nshipped: {pm.result}")
    print(f"workspace: {WORKSPACE}/main.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
