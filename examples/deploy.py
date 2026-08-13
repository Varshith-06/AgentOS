"""Human approval as a first-class kernel object.

    python -m agentos.cli run examples/deploy.py
    python -m agentos.cli approvals                          # second terminal
    python -m agentos.cli approve 1 --as "Senior Engineer"

The wrong role is refused, and the approval is durable: kill this process
while it is blocked and the next run re-attaches instead of asking twice.
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
        )

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
