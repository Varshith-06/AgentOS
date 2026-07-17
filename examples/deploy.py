"""Human approval as a first-class kernel object (AgentOS.pdf p.5-6).

The Deployer runs its checks, then declares that it needs a human:

    approval = await ctx.request_approval(
        role="Senior Engineer", reason="Production deployment"
    )

The runtime blocks the process — `agent ps` shows Blocked, waiting on Senior
Engineer — and the human becomes a node in the dependency graph, identical in
kind to an agent, an event, or a timer. Nothing polls. When the grant lands,
the scheduler wakes the Deployer exactly where it stopped.

Run it:            python -m agentos.cli run examples/deploy.py
Watch it block:    python -m agentos.cli ps          (second terminal)
See the request:   python -m agentos.cli approvals
Approve it:        python -m agentos.cli approve 1 --as "Senior Engineer"

Approving as the wrong role is refused — try --as "Intern".

The approval is durable, not a callback. Kill this process (Ctrl-C) while it is
blocked and run it again: the Deployer re-attaches to the same pending approval
instead of asking twice. Approve while nothing is running, then start the run:
it sails through.
"""

from __future__ import annotations

import asyncio

from agentos import Agent, Kernel


class Deployer(Agent):
    priority = "High"

    async def run(self, ctx):
        await ctx.log("running the test suite")
        await ctx.sleep(0.5)
        await ctx.log("tests green - production needs a human decision")

        approval = await ctx.request_approval(
            role="Senior Engineer", reason="Production deployment"
        )  # state: Blocked, until a human grants it

        await ctx.log(f"approved by {approval['by']} - deploying")
        await ctx.sleep(0.5)
        return {"deployed": True, "approved_by": approval["by"]}


async def main(slots: int = 4, policy: str = "fifo") -> int:
    kernel = Kernel(policy=policy, slots=slots)
    print("The Deployer will block on a human. From another terminal:")
    print('  python -m agentos.cli ps')
    print('  python -m agentos.cli approve 1 --as "Senior Engineer"')
    print()

    result = await kernel.run_until_done(Deployer())
    print(f"\nDeployer returned: {result}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
