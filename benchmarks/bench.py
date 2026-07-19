"""The evaluation (Phase 8, p.17-18): numbers, not assertions.

Measures the three claims from the design doc, offline and deterministically:

  1. RECOVERY   a hard kill mid-run costs the work since the last completed
                syscall and nothing more — count re-executed steps (target: 0)
  2. APPROVAL   human-in-the-loop latency: approve() -> agent finished
  3. LOAD       multiple applications on one daemon: throughput, and a cost
                ledger that is exact to the token

Run it:   python benchmarks/bench.py

The harness measures AgentOS against its own claims; a LangGraph or CrewAI
comparator can be slotted in as another column by anyone with those installed
— the workloads here are deliberately framework-shaped (step pipelines, an
approval gate, N clients x M agents), not AgentOS-shaped.
"""

from __future__ import annotations

import asyncio
import statistics
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agentos import Agent, Kernel, RuntimeClient  # noqa: E402
from agentos.kernel.memory import MemoryManager  # noqa: E402
from agentos.kernel.states import AgentState  # noqa: E402
from agentos.kernel.store import Store  # noqa: E402
from agentos.runtime.daemon import Daemon  # noqa: E402

TICK = 0.01
MOCK = {"classes": {"fast": [
    {"provider": "mock", "model": "mock-fast", "cost_per_mtok": [1.0, 5.0]},
]}}


# -- workload agents ----------------------------------------------------------

class StepWorker(Agent):
    """N slow steps; each step is counted in longterm memory, so a step that
    ran twice is arithmetically visible."""

    async def run(self, ctx):
        tag, steps = self.params["tag"], self.params["steps"]
        for i in range(steps):
            key = f"{tag}-step-{i}"
            n = (await ctx.memory.retrieve(key, kind="longterm")) or 0
            await ctx.memory.store(key, n + 1, kind="longterm")
            await ctx.sleep(0.05)
        return {"tag": tag, "steps": steps}


class Gated(Agent):
    async def run(self, ctx):
        await ctx.request_approval(role="Operator", reason="benchmark")
        return "resumed"


class ModelCaller(Agent):
    async def run(self, ctx):
        for _ in range(self.params["calls"]):
            await ctx.request_model("fast", prompt=self.params["prompt"])
        return "done"


# -- 1. recovery after a hard kill --------------------------------------------

async def bench_recovery(agents: int = 3, steps: int = 6) -> dict:
    tmp = tempfile.TemporaryDirectory()
    store = Store(tmp.name)
    k1 = Kernel(store=store, tick=TICK)
    for a in range(agents):
        k1.spawn(StepWorker(tag=f"w{a}", steps=steps))
    run1 = asyncio.create_task(k1.run())
    await asyncio.sleep(steps * 0.05 / 2)  # die roughly halfway through

    # kill -9, in effect: swap the store for a scratch one so the death throes
    # cannot touch persisted state, then cancel every task with no cleanup.
    scratch_dir = tempfile.TemporaryDirectory()
    scratch = Store(scratch_dir.name)
    k1.store, k1.memory = scratch, MemoryManager(scratch)
    tasks = [run1] + [p.task for p in k1.table.all() if p.task]
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    scratch.close()

    journaled = sum(len(v) for v in store.load_journals().values())
    done_before = _steps_done(store)

    t0 = time.perf_counter()
    k2 = Kernel(store=store, tick=TICK, recover=True)
    await k2.run()
    recovery_s = time.perf_counter() - t0

    counters = _step_counters(store)
    result = {
        "total_steps": agents * steps,
        "done_before_crash": done_before,
        "journaled_syscalls": journaled,
        "re_executed": sum(v - 1 for v in counters if v > 1),
        "recovery_s": recovery_s,
        "all_finished": all(not p.alive for p in k2.table.all()),
    }
    store.close()
    tmp.cleanup(), scratch_dir.cleanup()
    return result


def _step_counters(store: Store) -> list[int]:
    rows = store.db.execute(
        "SELECT value FROM memory WHERE mtype = 'longterm'"
    ).fetchall()
    return [int(r["value"]) for r in rows]


def _steps_done(store: Store) -> int:
    return len(_step_counters(store))


# -- 2. human-in-the-loop latency ---------------------------------------------

async def bench_approval(rounds: int = 5) -> dict:
    latencies = []
    for _ in range(rounds):
        tmp = tempfile.TemporaryDirectory()
        store = Store(tmp.name)
        k = Kernel(store=store, tick=TICK)
        pid = k.spawn(Gated())
        run = asyncio.create_task(k.run())
        while k.table.get(pid).state is not AgentState.BLOCKED:
            await asyncio.sleep(0.005)
        t0 = time.perf_counter()
        k.approve(pid, "Operator")
        await run
        latencies.append((time.perf_counter() - t0) * 1000)
        store.close()
        tmp.cleanup()
    return {"rounds": rounds, "median_ms": statistics.median(latencies),
            "worst_ms": max(latencies)}


# -- 3. cost under multi-application load ---------------------------------------

async def bench_load(apps: int = 3, agents_per_app: int = 5, calls: int = 2) -> dict:
    tmp = tempfile.TemporaryDirectory()
    store = Store(tmp.name)
    daemon = Daemon(store=store, port=0, tick=TICK, models=MOCK)
    task = asyncio.create_task(daemon.start())
    await asyncio.sleep(0.05)

    prompt = "six words of deterministic benchmark prompt"
    t0 = time.perf_counter()

    def one_app() -> list:
        client = RuntimeClient(url=daemon.url)
        pids = [
            client.submit(ModelCaller(calls=calls, prompt=prompt))
            for _ in range(agents_per_app)
        ]
        return [client.wait(pid, timeout=60) for pid in pids]

    await asyncio.gather(*(asyncio.to_thread(one_app) for _ in range(apps)))
    wall = time.perf_counter() - t0

    ledger = store.model_costs()
    total_calls = apps * agents_per_app * calls
    words = len(prompt.split())
    # mock provider: input tokens = words in, output = words of its fixed reply
    per_call = next(iter(ledger.values()))["cost"] / calls
    expected = per_call * total_calls
    total_cost = sum(c["cost"] for c in ledger.values())

    daemon.stop()
    await asyncio.wait_for(task, timeout=10)
    store.close()
    tmp.cleanup()
    return {
        "apps": apps,
        "agents": apps * agents_per_app,
        "model_calls": total_calls,
        "wall_s": wall,
        "agents_per_s": (apps * agents_per_app) / wall,
        "ledger_usd": total_cost,
        "ledger_exact": abs(total_cost - expected) < 1e-12,
    }


# -- the table -----------------------------------------------------------------

def row(label: str, value, note: str = "") -> None:
    print(f"  {label:<38} {value:>14}   {note}")


async def main() -> int:
    print("AgentOS benchmark (offline, mock models, tick=10ms)\n")

    r = await bench_recovery()
    print("1. recovery after a hard kill")
    row("steps total", r["total_steps"])
    row("steps done before the kill", r["done_before_crash"])
    row("journaled syscalls replayed", r["journaled_syscalls"])
    row("steps re-executed after recovery", r["re_executed"], "<- the claim: 0")
    row("recovery wall time", f"{r['recovery_s']:.2f}s", "replay + remaining work")
    row("every agent finished", str(r["all_finished"]))

    a = await bench_approval()
    print("\n2. human-in-the-loop latency (approve -> agent finished)")
    row("rounds", a["rounds"])
    row("median", f"{a['median_ms']:.1f}ms")
    row("worst", f"{a['worst_ms']:.1f}ms")

    l = await bench_load()
    print("\n3. cost under multi-application load (one shared daemon)")
    row("applications", l["apps"])
    row("agents", l["agents"])
    row("model calls", l["model_calls"])
    row("wall time", f"{l['wall_s']:.2f}s")
    row("throughput", f"{l['agents_per_s']:.1f} agents/s")
    row("ledger total", f"${l['ledger_usd']:.6f}")
    row("ledger exact to the token", str(l["ledger_exact"]), "<- the claim")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
