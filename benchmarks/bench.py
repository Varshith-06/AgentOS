"""The evaluation: recovery after a hard kill, approval latency, cost under load."""

from __future__ import annotations

import argparse
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


class StepWorker(Agent):
    """N slow steps, each counted in longterm memory, so a repeat is visible."""

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


async def bench_recovery(agents: int = 3, steps: int = 6) -> dict:
    tmp = tempfile.TemporaryDirectory()
    store = Store(tmp.name)
    k1 = Kernel(store=store, tick=TICK)
    for a in range(agents):
        k1.spawn(StepWorker(tag=f"w{a}", steps=steps))
    run1 = asyncio.create_task(k1.run())

    # Watch the work and pull the plug at the halfway mark: a fixed delay that
    # is mid-run on a fast machine kills an empty runtime on a slow one.
    halfway = agents * steps // 2

    async def until_halfway() -> None:
        while _steps_done(store) < halfway:
            await asyncio.sleep(0.01)

    await asyncio.wait_for(until_halfway(), timeout=120)

    # kill -9, in effect: swap in a scratch store so the death throes cannot
    # touch persisted state, then cancel every task with no cleanup.
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


def row(label: str, value, note: str = "") -> None:
    print(f"  {label:<38} {value:>14}   {note}")


async def main(check: bool = False) -> int:
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

    if not check:
        return 0

    # --check gates on the correctness claims only. Timings are machine-
    # dependent, so they are printed and never asserted.
    failures = []
    if r["re_executed"] != 0:
        failures.append(f"{r['re_executed']} step(s) re-executed after recovery, expected 0")
    if not r["all_finished"]:
        failures.append("not every agent finished after recovery")
    if not l["ledger_exact"]:
        failures.append("cost ledger did not match the token-exact expectation")

    print()
    if failures:
        for f in failures:
            print(f"  FAIL  {f}")
        return 1
    print("  claims hold: 0 work re-executed, all agents finished, ledger exact.")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true",
                    help="exit non-zero if a correctness claim regressed (timings are not asserted)")
    raise SystemExit(asyncio.run(main(ap.parse_args().check)))
