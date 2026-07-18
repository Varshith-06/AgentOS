"""AgentOS vs LangGraph, same workloads, same machine, same durability backend.

The three benchmarks in bench.py measure AgentOS against its own claims. This
one puts a widely-used comparator in the next column. It is deliberately
unkind to AgentOS where AgentOS is slower, because a benchmark you cannot
publish next to the competitor's source is not a benchmark.

Run it:   python benchmarks/compare.py        (skips cleanly if no langgraph)

What is held equal
------------------
* Durability backend: SQLite on both sides (AgentOS's syscall journal,
  LangGraph's SqliteSaver checkpointer).
* The billable operation: one INSERT into a shared tally table, plus a fixed
  80ms delay. Nothing touches the network, so this measures the frameworks,
  not a model provider.
* The crash: a real OS kill of a real process, delivered by the parent once
  the tally shows the same amount of completed work on both sides. Nothing is
  simulated in-process.

The LangGraph workload is written twice, on purpose:
  langgraph (loop)   one node that loops over the six calls — how the workload
                     reads if you write it the obvious way
  langgraph (nodes)  six nodes, one per call — the decomposition that buys
                     finer checkpoint granularity
That pair is the honest comparison: the question is not whether LangGraph
*can* checkpoint finely, it is what granularity you get for a given way of
expressing the work.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agentos import Agent, Kernel  # noqa: E402
from agentos.kernel.store import Store  # noqa: E402

CALLS = 6  # billable operations per run
KILL_AFTER = 3  # kill once this many have landed
OP_DELAY = 0.08  # seconds each billable operation takes
STEPS = 30  # durable steps for the overhead measurement
TICK = 0.005  # AgentOS scheduler tick for the comparison
REAL_WORK = 0.6  # a stand-in for what one real model call costs

try:
    from langgraph.graph import StateGraph, START, END
    from langgraph.checkpoint.sqlite import SqliteSaver
    from langgraph.types import interrupt, Command
    HAVE_LG = True
except ImportError:  # comparator absent: AgentOS columns still run
    HAVE_LG = False


# -- the shared tally: the only source of truth about what actually ran -------

def tally_init(path: str) -> None:
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute("CREATE TABLE IF NOT EXISTS calls (idx INTEGER, ts REAL)")
    conn.close()


def tally_count(path: str) -> int:
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        return conn.execute("SELECT COUNT(*) FROM calls").fetchone()[0]
    finally:
        conn.close()


def tally_rows(path: str) -> list[int]:
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        return [r[0] for r in conn.execute("SELECT idx FROM calls").fetchall()]
    finally:
        conn.close()


# -- AgentOS workloads --------------------------------------------------------

class BillingAgent(Agent):
    """Six billable calls through the sql capability. Each crosses the syscall
    boundary, so each is journaled — and a journaled call does not re-execute."""

    async def run(self, ctx):
        for i in range(self.params["calls"]):
            await ctx.request_tool(
                "sql", "execute",
                statement="INSERT INTO calls VALUES (?, ?)",
                params=[i, time.time()],
            )
            await ctx.sleep(self.params["delay"])
        return {"done": self.params["calls"]}


class StepAgent(Agent):
    """N durable steps. With delay=0 this is pure framework overhead; with a
    realistic delay it shows what that overhead is worth next to real work."""

    async def run(self, ctx):
        for i in range(self.params["steps"]):
            await ctx.request_tool(
                "sql", "execute",
                statement="INSERT INTO calls VALUES (?, ?)",
                params=[i, time.time()],
            )
            if self.params["delay"]:
                await ctx.sleep(self.params["delay"])
        return {"steps": self.params["steps"]}


class GatedAgent(Agent):
    async def run(self, ctx):
        await ctx.request_approval(role="Operator", reason="comparison")
        return "resumed"


def _agentos_kernel(rundir: str, tally: str, recover: bool = False) -> Kernel:
    return Kernel(
        store=Store(rundir),
        tick=TICK,
        recover=recover,
        tools={"sql": {"db": tally}},
        permissions={"*": ["sql"]},
    )


# -- LangGraph workloads ------------------------------------------------------

def _lg_billing_loop(tally: str, calls: int, delay: float):
    """One node, six calls inside it: the obvious way to write the workload."""
    from typing import TypedDict

    class S(TypedDict):
        done: int

    def work(state: S) -> S:
        conn = sqlite3.connect(tally, isolation_level=None)
        for i in range(calls):
            conn.execute("INSERT INTO calls VALUES (?, ?)", (i, time.time()))
            time.sleep(delay)
        conn.close()
        return {"done": calls}

    g = StateGraph(S)
    g.add_node("work", work)
    g.add_edge(START, "work")
    g.add_edge("work", END)
    return g


def _lg_billing_nodes(tally: str, calls: int, delay: float):
    """One node per call: a checkpoint lands between every billable call."""
    from typing import TypedDict

    class S(TypedDict):
        done: int

    def make(i: int):
        def work(state: S) -> S:
            conn = sqlite3.connect(tally, isolation_level=None)
            conn.execute("INSERT INTO calls VALUES (?, ?)", (i, time.time()))
            conn.close()
            time.sleep(delay)
            return {"done": state.get("done", 0) + 1}
        return work

    g = StateGraph(S)
    prev = START
    for i in range(calls):
        name = f"call{i}"
        g.add_node(name, make(i))
        g.add_edge(prev, name)
        prev = name
    g.add_edge(prev, END)
    return g


def _lg_steps(tally: str, steps: int, delay: float = 0.0):
    from typing import TypedDict

    class S(TypedDict):
        n: int

    def make(i: int):
        def work(state: S) -> S:
            conn = sqlite3.connect(tally, isolation_level=None)
            conn.execute("INSERT INTO calls VALUES (?, ?)", (i, time.time()))
            conn.close()
            if delay:
                time.sleep(delay)
            return {"n": state.get("n", 0) + 1}
        return work

    g = StateGraph(S)
    prev = START
    for i in range(steps):
        name = f"s{i}"
        g.add_node(name, make(i))
        g.add_edge(prev, name)
        prev = name
    g.add_edge(prev, END)
    return g


def _lg_run(graph, ckpt: str, thread: str, resume: bool) -> None:
    conn = sqlite3.connect(ckpt, check_same_thread=False)
    app = graph.compile(checkpointer=SqliteSaver(conn))
    cfg = {"configurable": {"thread_id": thread}}
    app.invoke(None if resume else {"done": 0, "n": 0}, cfg)
    conn.close()


# -- child entry points (run in their own process so they can be killed) ------

def _child(kind: str, rundir: str, tally: str, resume: bool) -> int:
    if kind == "agentos":
        k = _agentos_kernel(rundir, tally, recover=resume)
        if not resume:
            k.spawn(BillingAgent(calls=CALLS, delay=OP_DELAY))
        asyncio.run(k.run())
    elif kind == "lg-loop":
        _lg_run(_lg_billing_loop(tally, CALLS, OP_DELAY),
                os.path.join(rundir, "ckpt.db"), "t", resume)
    elif kind == "lg-nodes":
        _lg_run(_lg_billing_nodes(tally, CALLS, OP_DELAY),
                os.path.join(rundir, "ckpt.db"), "t", resume)
    return 0


# -- 1. recovery granularity --------------------------------------------------

def bench_recovery(kind: str) -> dict:
    """Start the workload, kill it hard once KILL_AFTER calls have landed,
    recover it, and count how many billable calls ran more than once."""
    tmp = tempfile.mkdtemp()
    rundir = os.path.join(tmp, "run")
    os.makedirs(rundir, exist_ok=True)
    tally = os.path.join(tmp, "tally.db")
    tally_init(tally)

    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]))
    args = [sys.executable, "-X", "utf8", os.path.abspath(__file__),
            "--child", kind, "--rundir", rundir, "--tally", tally]

    proc = subprocess.Popen(args, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    deadline = time.time() + 60
    while time.time() < deadline:
        if tally_count(tally) >= KILL_AFTER:
            break
        if proc.poll() is not None:
            break
        time.sleep(0.01)
    before = tally_count(tally)
    proc.kill()  # TerminateProcess / SIGKILL: no cleanup, no flush, no mercy
    proc.wait(timeout=30)

    t0 = time.perf_counter()
    rec = subprocess.run(args + ["--resume"], env=env,
                         capture_output=True, timeout=180)
    recovery_s = time.perf_counter() - t0
    ok = rec.returncode == 0

    rows = tally_rows(tally)
    total = len(rows)
    return {
        "calls": CALLS,
        "before_kill": before,
        "total_executions": total,
        "repeated": max(0, total - CALLS),
        "recovery_s": recovery_s,
        "completed": ok and total >= CALLS,
        "err": rec.stderr.decode("utf-8", "replace")[-300:] if not ok else "",
    }


# -- 2. cost of a durable step ------------------------------------------------

async def bench_steps_agentos(delay: float = 0.0, tick: float = TICK) -> dict:
    tmp = tempfile.mkdtemp()
    tally = os.path.join(tmp, "tally.db")
    tally_init(tally)
    k = Kernel(
        store=Store(os.path.join(tmp, "run")), tick=tick,
        tools={"sql": {"db": tally}}, permissions={"*": ["sql"]},
    )
    k.spawn(StepAgent(steps=STEPS, delay=delay))
    t0 = time.perf_counter()
    await k.run()
    wall = time.perf_counter() - t0
    return {"steps": STEPS, "wall_s": wall, "ms_per_step": wall / STEPS * 1000}


def bench_steps_langgraph(delay: float = 0.0) -> dict:
    tmp = tempfile.mkdtemp()
    tally = os.path.join(tmp, "tally.db")
    tally_init(tally)
    t0 = time.perf_counter()
    _lg_run(_lg_steps(tally, STEPS, delay), os.path.join(tmp, "ckpt.db"), "s", False)
    wall = time.perf_counter() - t0
    return {"steps": STEPS, "wall_s": wall, "ms_per_step": wall / STEPS * 1000}


# -- 3. human-in-the-loop round trip ------------------------------------------

async def bench_hitl_agentos(rounds: int = 5) -> dict:
    from agentos.kernel.states import AgentState
    lat = []
    for _ in range(rounds):
        tmp = tempfile.mkdtemp()
        tally = os.path.join(tmp, "tally.db")
        tally_init(tally)
        k = _agentos_kernel(os.path.join(tmp, "run"), tally)
        pid = k.spawn(GatedAgent())
        run = asyncio.create_task(k.run())
        while k.table.get(pid).state is not AgentState.BLOCKED:
            await asyncio.sleep(0.002)
        t0 = time.perf_counter()
        k.approve(pid, "Operator")
        await run
        lat.append((time.perf_counter() - t0) * 1000)
    return {"median_ms": statistics.median(lat), "worst_ms": max(lat)}


def bench_hitl_langgraph(rounds: int = 5) -> dict:
    from typing import TypedDict

    class S(TypedDict):
        v: str

    def gate(state: S) -> S:
        d = interrupt({"reason": "comparison"})
        return {"v": f"resumed:{d}"}

    g = StateGraph(S)
    g.add_node("gate", gate)
    g.add_edge(START, "gate")
    g.add_edge("gate", END)

    lat = []
    for i in range(rounds):
        tmp = tempfile.mkdtemp()
        conn = sqlite3.connect(os.path.join(tmp, "ckpt.db"), check_same_thread=False)
        app = g.compile(checkpointer=SqliteSaver(conn))
        cfg = {"configurable": {"thread_id": f"t{i}"}}
        app.invoke({"v": ""}, cfg)  # runs up to the interrupt
        t0 = time.perf_counter()
        app.invoke(Command(resume="ok"), cfg)  # approve -> finished
        lat.append((time.perf_counter() - t0) * 1000)
        conn.close()
    return {"median_ms": statistics.median(lat), "worst_ms": max(lat)}


# -- the table ----------------------------------------------------------------

def main() -> int:
    print(f"AgentOS vs LangGraph — same workload, SQLite durability both sides")
    print(f"(tick={TICK*1000:.0f}ms, {CALLS} billable calls, {OP_DELAY*1000:.0f}ms each,"
          f" killed after {KILL_AFTER})\n")
    if not HAVE_LG:
        print("  langgraph not installed — comparator columns skipped")
        print("  pip install langgraph langgraph-checkpoint-sqlite\n")

    print("1. recovery granularity: billable calls REPEATED after a hard kill")
    print(f"   {'framework':<22} {'ran before kill':>16} {'total ran':>11} "
          f"{'repeated':>10} {'recover':>9}")
    rows = [("agentos", "agentos")]
    if HAVE_LG:
        rows += [("langgraph (loop)", "lg-loop"), ("langgraph (nodes)", "lg-nodes")]
    results = {}
    for label, kind in rows:
        r = bench_recovery(kind)
        results[kind] = r
        note = "" if r["completed"] else f"  !! {r['err']}"
        print(f"   {label:<22} {r['before_kill']:>16} {r['total_executions']:>11} "
              f"{r['repeated']:>10} {r['recovery_s']:>8.2f}s{note}")
    print(f"\n   repeated = billable calls that executed twice — work redone,"
          f" and in a real\n   system, model spend paid twice. Lower is better;"
          f" {CALLS} calls total.\n")

    print(f"2. cost of one durable step ({STEPS} steps, no delay: pure overhead)")
    bare = {}
    for tick in (0.001, 0.005, 0.02):
        a = asyncio.run(bench_steps_agentos(tick=tick))
        bare[tick] = a["ms_per_step"]
        print(f"   {'agentos tick=' + str(int(tick*1000)) + 'ms':<22} "
              f"{a['wall_s']:>8.2f}s  {a['ms_per_step']:>7.1f} ms/step")
    if HAVE_LG:
        l = bench_steps_langgraph()
        bare["lg"] = l["ms_per_step"]
        print(f"   {'langgraph':<22} {l['wall_s']:>8.2f}s  "
              f"{l['ms_per_step']:>7.1f} ms/step")
        gap = bare[0.001] - bare["lg"]
        print(f"\n   AgentOS pays ~{gap:.0f}ms more per durable step at its best tick.")

    print(f"\n2b. the same steps when each does {int(REAL_WORK*1000)}ms of real work")
    print("    (what a model call costs) — overhead as a share of the total")
    ar = asyncio.run(bench_steps_agentos(delay=REAL_WORK, tick=0.001))
    print(f"   {'agentos':<22} {ar['wall_s']:>8.2f}s  {ar['ms_per_step']:>7.1f} ms/step")
    if HAVE_LG:
        lr = bench_steps_langgraph(delay=REAL_WORK)
        print(f"   {'langgraph':<22} {lr['wall_s']:>8.2f}s  "
              f"{lr['ms_per_step']:>7.1f} ms/step")
        delta = (ar["wall_s"] - lr["wall_s"]) / lr["wall_s"] * 100
        print(f"\n   AgentOS is {delta:+.1f}% wall time on realistic work.")

    print("\n3. human-in-the-loop: approve -> agent finished")
    ha = asyncio.run(bench_hitl_agentos())
    print(f"   {'agentos':<22} {ha['median_ms']:>8.1f}ms median  "
          f"{ha['worst_ms']:>7.1f}ms worst")
    if HAVE_LG:
        hl = bench_hitl_langgraph()
        print(f"   {'langgraph':<22} {hl['median_ms']:>8.1f}ms median  "
              f"{hl['worst_ms']:>7.1f}ms worst")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--child")
    ap.add_argument("--rundir")
    ap.add_argument("--tally")
    ap.add_argument("--resume", action="store_true")
    ns = ap.parse_args()
    if ns.child:
        raise SystemExit(_child(ns.child, ns.rundir, ns.tally, ns.resume))
    raise SystemExit(main())
