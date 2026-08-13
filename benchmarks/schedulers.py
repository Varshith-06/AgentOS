"""The three scheduling policies, measured against each other.

    python benchmarks/schedulers.py

Each policy runs three workloads: independent agents (what a policy costs),
mixed urgency (how long an urgent agent waits), and a bottleneck (how long
agents blocked on it stay blocked). Re-queueing is what makes the last two
interesting: a yielding agent lands at the back of the ready queue, and
only the non-FIFO policies may jump it.
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


async def _run(policy: str, slots: int, build) -> tuple[Kernel, float, float]:
    """Run one workload under one policy. Returns (kernel, t0, wall)."""
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


async def bench_urgency(policy: str, per_band: int = 5, slots: int = 2) -> dict:
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
        "low_mean_s": statistics.mean(low),
    }


async def bench_bottleneck(
    policy: str,
    bottlenecks: int = 2,
    blocked_each: int = 3,
    filler: int = 20,
    slots: int = 2,
) -> dict:
    # The filler queue has to be long: FIFO round-robins back to the bottleneck
    # often enough that jumping it buys almost nothing when the queue is short.
    def build(k):
        targets = [k.spawn(Bottleneck(steps=4)) for _ in range(bottlenecks)]
        for target in targets:
            for _ in range(blocked_each):
                k.spawn(Blocked(target=target, steps=4))
        for i in range(filler):
            k.spawn(Work(tag=f"filler{i}", steps=12))

    k, t0, wall = await _run(policy, slots, build)
    blocked = _turnaround(k, t0, lambda p: p.name == "Blocked")
    blockers = _turnaround(k, t0, lambda p: p.name == "Bottleneck")
    return {
        "wall_s": wall,
        "blocker_mean_s": statistics.mean(blockers),
        "blocked_mean_s": statistics.mean(blocked),
        "blocked_worst_s": max(blocked),
    }


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
