"""Application #2 — another thin client, same shared runtime (Phase 7, p.8).

Run alongside app_research.py: two independent applications, two terminals,
one process table, one cost ledger. Neither application knows the other
exists — but `agent ps` sees both, which is the p.8 claim.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # repo-checkout runs

from agentos import Agent, RuntimeClient  # noqa: E402


class SupportTriage(Agent):
    async def run(self, ctx):
        ticket = self.params["ticket"]
        reply = await ctx.request_model(
            "fast", prompt=f"Classify this support ticket in one word: {ticket!r}"
        )
        await ctx.log(f"ticket triaged by {reply['model']}")
        await ctx.sleep(3)  # linger so `agent ps` catches both apps live
        return {"ticket": ticket, "triage": reply["text"][:80], "cost": reply["cost"]}


def main() -> int:
    client = RuntimeClient()
    pids = [
        client.submit(SupportTriage(ticket="app crashes on login")),
        client.submit(SupportTriage(ticket="invoice missing VAT")),
    ]
    print(f"submitted {len(pids)} SupportTriage agents: pids {pids}")
    for pid in pids:
        print(f"  pid {pid}: {client.wait(pid)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
