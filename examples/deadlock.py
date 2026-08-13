"""Two agents waiting on each other. The kernel must say so, not hang.

    python -m agentos.cli run examples/deadlock.py
"""

from __future__ import annotations

import asyncio

from agentos import Agent, Kernel, KernelError


class Stubborn(Agent):
    """Waits on the agent that is waiting on it."""

    async def run(self, ctx):
        target = self.params["target"]
        await ctx.log(f"waiting on pid {target}, which is waiting on me")
        try:
            await ctx.wait(target)
        except KernelError as exc:
            await ctx.log(f"kernel refused the wait: {exc}")
            return {"deadlock_detected": True}
        return {"deadlock_detected": False}


class Circular(Agent):
    async def run(self, ctx):
        other = await ctx.spawn(Stubborn(target=ctx.pid))
        return await ctx.wait(other)


class Hopeful(Agent):
    """Waits for an event that nobody will ever publish."""

    async def run(self, ctx):
        await ctx.log("waiting for GodotArrived")
        await ctx.wait_event("GodotArrived")
        return "never reached"


async def main(slots: int = 4, policy: str = "fifo") -> int:
    print("=== 1. a cycle in the wait-for graph ===")
    k1 = Kernel(policy=policy, slots=slots)
    result = await asyncio.wait_for(k1.run_until_done(Circular()), timeout=5)
    print(f"  Circular got back: {result}")
    for row in k1.store.processes():
        print(f"  pid {row['pid']} {row['name']:<10} {row['status']:<9} {row['exit_reason']}")

    print("\n=== 2. waiting on an event nobody will publish ===")
    k2 = Kernel(policy=policy, slots=slots, store=k1.store)
    await asyncio.wait_for(k2.run_until_done(Hopeful()), timeout=5)
    for row in k2.store.processes():
        print(f"  pid {row['pid']} {row['name']:<10} {row['status']:<9} {row['exit_reason']}")

    print("\nNeither run hung. Both were reported.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
