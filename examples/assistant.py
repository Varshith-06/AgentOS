"""The first LLM call — and the proof that model choice is runtime config (p.7).

The Assistant asks for a capability class. It does not know what a Claude is:

    reply = await ctx.request_model("fast", prompt=...)

Routing lives in .agentos/models.json (seeded below on first run). The "fast"
chain is: Claude Haiku 4.5 if ANTHROPIC_API_KEY is set -> a local
OpenAI-compatible server if one is listening on :11434 (e.g. Ollama) -> the
offline mock provider, which always works. Set or unset the key, start or stop
Ollama, re-run: the same agent code lands on a different model, and the cost
lands in the COST column of `agent ps`.

Run it:        python -m agentos.cli run examples/assistant.py
Cost:          python -m agentos.cli ps       (while it runs)  or  agent logs
Swap models:   edit .agentos/models.json — no agent code changes
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
