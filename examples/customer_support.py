"""Customer support (Phase 8, p.10) — events in, routed work out.

An Intake agent publishes each incoming ticket as an event; it has no idea who
handles tickets. Triage subscribes, classifies each one with a model call, and
spawns the specialists with their queues. Every resolution lands in shared
memory, which is how the Supervisor compiles the report without ever touching
a specialist.

Run it:   python -m agentos.cli run examples/customer_support.py
Timeline: python -m agentos.cli events -v
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from agentos import Agent, Kernel
from agentos.kernel.models import DEFAULT_MODELS_CONFIG

MODELS = Path(".agentos/models.json")

TICKETS = [
    "I was double-charged on my last invoice",
    "the app crashes when I upload a file",
    "please add a dark mode",
]


class Intake(Agent):
    async def run(self, ctx):
        await ctx.sleep(0.05)  # let Triage subscribe first
        for ticket in self.params["tickets"]:
            await ctx.publish("TicketReceived", text=ticket)
        return {"received": len(self.params["tickets"])}


class Triage(Agent):
    priority = "High"

    async def run(self, ctx):
        await ctx.subscribe("TicketReceived")
        queues: dict[str, list[str]] = {"billing": [], "tech": []}
        for _ in range(self.params["expected"]):
            event = await ctx.wait_event("TicketReceived")
            reply = await ctx.request_model(
                "fast",
                prompt=f"Answer 'billing' or 'tech' only. Ticket: {event['text']!r}",
            )
            kind = "billing" if "billing" in reply["text"].lower() else "tech"
            queues[kind].append(event["text"])
            await ctx.log(f"routed to {kind}: {event['text'][:40]!r}")
        pids = [
            await ctx.spawn(Specialist(team=team, tickets=tickets))
            for team, tickets in queues.items()
            if tickets
        ]
        await ctx.wait_all(agents=pids)
        return {t: len(q) for t, q in queues.items()}


class Specialist(Agent):
    async def run(self, ctx):
        team = self.params["team"]
        for ticket in self.params["tickets"]:
            reply = await ctx.request_model(
                "fast", prompt=f"One-line resolution for: {ticket!r}"
            )
            await ctx.memory.store(
                f"resolved:{ticket[:30]}",
                {"team": team, "resolution": reply["text"][:80]},
                kind="shared",
            )
        return {"team": team, "resolved": len(self.params["tickets"])}


class Supervisor(Agent):
    priority = "High"

    async def run(self, ctx):
        triage = await ctx.spawn(Triage(expected=len(TICKETS)))
        await ctx.spawn(Intake(tickets=TICKETS))
        routed = await ctx.wait(triage)
        resolutions = await ctx.memory.retrieve(kind="shared")
        return {"routed": routed, "resolved": len(resolutions)}


async def main(slots: int = 4, policy: str = "fifo") -> int:
    if not MODELS.exists():
        MODELS.parent.mkdir(parents=True, exist_ok=True)
        MODELS.write_text(json.dumps(DEFAULT_MODELS_CONFIG, indent=2), encoding="utf-8")

    kernel = Kernel(policy=policy, slots=slots)
    result = await kernel.run_until_done(Supervisor())
    print(f"\nsupport report: {result}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
