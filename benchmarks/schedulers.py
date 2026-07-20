"""Scheduling policies, measured against each other (p.4).

AgentOS ships three policies. The published benchmark numbers all use `fifo`,
the default; this harness asks what the other two are actually worth.

The honest way to compare schedulers is not to find one number where one wins.
A scheduling policy is a choice about *which work matters*, so each policy is
run against three workloads, and each workload is the kind of load a different
policy was designed for:

  A. INDEPENDENT   agents that need nothing from each other. Nothing to
                   optimise, so this measures what a policy *costs*.
  B. MIXED URGENCY High/Normal/Low submitted together, more work than slots.
                   Measures how long an urgent agent waits behind routine work.
  C. BOTTLENECK    a few agents that many others are blocked on, plus filler
                   nobody is waiting for. Measures how long the blocked agents
                   stay blocked.

The mechanism that makes B and C interesting is re-queueing. An agent that
sleeps goes Sleeping -> Ready -> the *back* of the ready queue, so a bottleneck
agent that yields several times keeps landing behind the filler. FIFO honours
that queue; the other two policies are allowed to jump it.

Run it:   python benchmarks/schedulers.py
"""

from __future__ import annotations

import asyncio
import statistics
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agentos import Agent, Kernel  # noqa: E402
from agentos.kernel.scheduler import POLICIES  # noqa: E402
from agentos.kernel.store import Store  # noqa: E402

TICK = 0.01
STEP = 0.02          # one unit of agent work
POLICY_ORDER = ["fifo", "priority", "dependency"]


# -- workload agents ----------------------------------------------------------

class Work(Agent):
    """Fixed work in yielding steps, so the scheduler sees it more than once."""

    async def run(self, ctx):
        for _ in range(self.params["steps"]):
            await ctx.sleep(STEP)
        return self.params["tag"]


class Bottleneck(Agent):
    """Work that other agents are blocked on."""

    async def run(self, ctx):
        for _ in range(self.params["steps"]):
            await ctx.sleep(STEP)
        return "released"


class Blocked(Agent):
    """Waits for a bottleneck to finish, then does its own work."""

    async def run(self, ctx):
        await ctx.wait(self.params["target"])
        for _ in range(self.params["steps"]):
            await ctx.sleep(STEP)
        return "done"


# -- harness ------------------------------------------------------------------

async def _run(policy: str, slots: int, build) -> tuple[Kernel, float, float]:
    """Run one workload under one policy. `build` spawns and returns nothing;
    it receives the kernel. Returns (kernel, t0, wall)."""
    tmp = tempfile.TemporaryDirectory()
    store = Store(tmp.name)
    k = Kernel(store=store, tick=TICK, policy=policy, slots=slots)
    build(k)
    t0 = time.time()
    start = time.perf_counter()
    await asyncio.wait_for(k.run(), timeout=300)
    wall = time.perf_counter() - start
    store.close()
    tmp.cleanup()
    return k, t0, wall


def _turnaround(k: Kernel, t0: float, predicate) -> list[float]:
    """Seconds from run start to finish, for the processes we care about."""
    return [
        (p.ended_at - t0)
        for p in k.table.all()
        if p.ended_at is not None and predicate(p)
    ]


# -- A. independent agents: what does a policy cost? --------------------------

async def bench_independent(policy: str, agents: int = 24, slots: int = 4) -> dict:
    def build(k):
        for i in range(agents):
            k.spawn(Work(tag=f"w{i}", steps=3))

    k, _t0, wall = await _run(policy, slots, build)
    return {
        "wall_s": wall,
        "agents_per_s": agents / wall,
        "finished": sum(1 for p in k.table.all() if not p.alive),
    }


# -- B. mixed urgency: how long does an urgent agent wait? --------------------

async def bench_urgency(policy: str, per_band: int = 5, slots: int = 2) -> dict:
    # Submitted worst-case for the urgent work: every Low and Normal agent is
    # already in the queue before the first High one arrives.
    def build(k):
        for band in ("Low", "Normal", "High"):
            for i in range(per_band):
                agent = Work(tag=f"{band}{i}", steps=4)
                agent.priority = band
                k.spawn(agent)

    k, t0, wall = await _run(policy, slots, build)
    high = _turnaround(k, t0, lambda p: p.priority == "High")
    low = _turnaround(k, t0, lambda p: p.priority == "Low")
    return {
        "wall_s": wall,
        "high_mean_s": statistics.mean(high),
        "high_worst_s": max(high),
        "low_mean_s": statistics.mean(low),  # the cost of the preference
    }


# -- C. bottleneck: how long do blocked agents stay blocked? ------------------

async def bench_bottleneck(
    policy: str,
    bottlenecks: int = 2,
    blocked_each: int = 3,
    filler: int = 20,
    slots: int = 2,
) -> dict:
    # The filler queue has to be long for this to be a real question. FIFO
    # round-robins, so a short queue comes back to the bottleneck often enough
    # that jumping it buys almost nothing; the starvation only bites when the
    # bottleneck waits behind a full cycle of everyone else for every step.
    def build(k):
        # Bottlenecks first so their pids exist to be waited on; the contrast
        # between policies comes from re-queueing, not from this order.
        targets = [k.spawn(Bottleneck(steps=4)) for _ in range(bottlenecks)]
        for target in targets:
            for _ in range(blocked_each):
                k.spawn(Blocked(target=target, steps=4))
        for i in range(filler):
            k.spawn(Work(tag=f"filler{i}", steps=12))

    k, t0, wall = await _run(policy, slots, build)
    blocked = _turnaround(k, t0, lambda p: p.name == "Blocked")
    # Did the policy actually front-run the bottleneck? That is the mechanism;
    # whether it improves the makespan is a separate question, since the total
    # work is identical either way.
    blockers = _turnaround(k, t0, lambda p: p.name == "Bottleneck")
    return {
        "wall_s": wall,
        "blocker_mean_s": statistics.mean(blockers),
        "blocked_mean_s": statistics.mean(blocked),
        "blocked_worst_s": max(blocked),
    }


# -- the table ----------------------------------------------------------------

def table(title: str, subtitle: str, columns: list[str], rows: dict) -> None:
    print(f"\n{title}")
    print(f"  {subtitle}")
    head = "  " + f"{'policy':<12}" + "".join(f"{c:>18}" for c in columns)
    print(head)
    print("  " + "-" * (len(head) - 2))
    for policy in POLICY_ORDER:
        cells = "".join(f"{v:>18}" for v in rows[policy])
        print(f"  {policy:<12}{cells}")


async def main() -> int:
    assert set(POLICY_ORDER) == set(POLICIES), "a policy was added but not benchmarked"
    print("AgentOS scheduling policies (offline, tick=10ms, "
          f"step={int(STEP * 1000)}ms)")

    independent = {p: await bench_independent(p) for p in POLICY_ORDER}
    table(
        "A. independent agents (24 agents, 4 slots)",
        "nothing to optimise: this is what the policy costs",
        ["wall", "agents/s"],
        {
            p: [f"{r['wall_s']:.2f}s", f"{r['agents_per_s']:.1f}"]
            for p, r in independent.items()
        },
    )

    urgency = {p: await bench_urgency(p) for p in POLICY_ORDER}
    table(
        "B. mixed urgency (5 High / 5 Normal / 5 Low, 2 slots)",
        "High submitted last, behind everything: mean time to finish",
        ["High mean", "High worst", "Low mean"],
        {
            p: [
                f"{r['high_mean_s']:.2f}s",
                f"{r['high_worst_s']:.2f}s",
                f"{r['low_mean_s']:.2f}s",
            ]
            for p, r in urgency.items()
        },
    )

    bottleneck = {p: await bench_bottleneck(p) for p in POLICY_ORDER}
    table(
        "C. bottleneck (2 blockers x 3 blocked agents + 20 filler, 2 slots)",
        "when the blocker clears, and how long its dependants stay blocked",
        ["blocker done", "blocked mean", "blocked worst", "wall"],
        {
            p: [
                f"{r['blocker_mean_s']:.2f}s",
                f"{r['blocked_mean_s']:.2f}s",
                f"{r['blocked_worst_s']:.2f}s",
                f"{r['wall_s']:.2f}s",
            ]
            for p, r in bottleneck.items()
        },
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
