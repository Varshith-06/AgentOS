"""The first LLM call, and the proof that model choice is runtime config.

    python -m agentos.cli run examples/assistant.py
    python -m agentos.cli ps                 # the cost, while it runs

The `fast` chain is seeded below: a frontier model if ANTHROPIC_API_KEY is
set, else a local server on :11434, else the offline mock. Editing
.agentos/models.json changes which one runs; the agent code does not.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from agentos import Agent, Kernel
from agentos.kernel.models import DEFAULT_MODELS_CONFIG as DEFAULT_CONFIG

MODELS = Path(".agentos/models.json")


class Assistant(Agent):
    async def run(self, ctx):
        reply = await ctx.request_model(
            "fast",
            prompt="In one sentence: why should AI agents be processes, not objects?",
            system="You are terse and precise.",
        )
        await ctx.log(f"served by {reply['model']} for ${reply['cost']:.4f}")
        return {
            "model": reply["model"],
            "provider": reply["provider"],
            "cost": reply["cost"],
            "text": reply["text"],
        }


async def main(slots: int = 4, policy: str = "fifo") -> int:
    if not MODELS.exists():
        MODELS.parent.mkdir(parents=True, exist_ok=True)
        MODELS.write_text(json.dumps(DEFAULT_CONFIG, indent=2) + "\n", encoding="utf-8")
        print(f"seeded routing config: {MODELS}\n")

    kernel = Kernel(policy=policy, slots=slots)
    result = await kernel.run_until_done(Assistant())

    print(f"\nserved by:  {result['provider']}:{result['model']}  (${result['cost']:.4f})")
    print(f"reply:      {result['text']}")
    print("\nset ANTHROPIC_API_KEY (or start Ollama) and re-run: same agent code,")
    print("different model. That is the whole point of Phase 5.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
