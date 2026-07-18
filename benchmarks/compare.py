"""AgentOS against the frameworks people actually use, on identical workloads.

  AgentOS  ·  LangGraph  ·  CrewAI Flows  ·  AutoGen  ·  Temporal

Run it:   python benchmarks/compare.py
Any comparator that is not installed is skipped, so this runs with none of
them present (AgentOS columns only) or all of them.

    pip install langgraph langgraph-checkpoint-sqlite crewai \
                autogen-core temporalio

What is held equal
------------------
* The billable operation: one INSERT into a shared tally table plus a fixed
  80ms of work. A shared table, not any framework's own logs, is what counts
  executions — nobody grades their own homework.
* Durability: every framework persists to SQLite. Temporal is the exception
  by design (its durability lives in a server process), which is itself part
  of the result.
* The crash: a real OS kill of a real process, delivered by the parent once
  the tally shows the same completed work everywhere. Nothing is simulated.

What is NOT held equal, and cannot be: these frameworks aim at different
things. CrewAI and AutoGen do not claim durable execution, so a recovery
number for them measures a capability they never advertised. Read the
recovery column as "what happens if the process dies", not as a scorecard.
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
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agentos import Agent, Kernel  # noqa: E402
from agentos.kernel.store import Store  # noqa: E402

CALLS = 6  # billable operations per recovery run
KILL_AFTER = 3  # kill once this many have landed
OP_DELAY = 0.08  # seconds each billable operation takes
STEPS = 30  # durable steps for the overhead measurement
REAL_WORK = 0.6  # stand-in for what one real model call costs
TICK = 0.005  # AgentOS scheduler tick


def have(mod: str) -> bool:
    from importlib.util import find_spec
    try:
        return find_spec(mod) is not None
    except (ImportError, ValueError):
        return False


HAVE = {
    "langgraph": have("langgraph"),
    "crewai": have("crewai"),
    "autogen": have("autogen_core"),
    "temporal": have("temporalio"),
}

# Framework imports stay inside the functions that use them: a child process
# doing an AgentOS run must not pay CrewAI's multi-second import, or the
# recovery timings measure this file's import graph instead of the runtimes.
# The timed measurements call _warm() first so no import lands inside a timer.


def _quiet():
    """Silence a framework's console UI during a timed run.

    CrewAI renders progress panels to stdout; in a real deployment you would
    turn that off, and leaving it on would charge terminal rendering to its
    per-step cost. Errors still surface — only stdout is captured.
    """
    import contextlib
    import io
    return contextlib.redirect_stdout(io.StringIO())


# -- the shared tally ---------------------------------------------------------

def tally_init(path: str) -> None:
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute("CREATE TABLE IF NOT EXISTS calls (idx INTEGER, ts REAL)")
    conn.close()


def tally_count(path: str) -> int:
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        return conn.execute("SELECT COUNT(*) FROM calls").fetchone()[0]
    except sqlite3.OperationalError:
        return 0
    finally:
        conn.close()


def bill(tally: str, i: int, delay: float = 0.0) -> None:
    conn = sqlite3.connect(tally, isolation_level=None)
    conn.execute("INSERT INTO calls VALUES (?, ?)", (i, time.time()))
    conn.close()
    if delay:
        time.sleep(delay)


# ============================ AgentOS =======================================

class BillingAgent(Agent):
    """Six billable calls through the sql capability. Each crosses the syscall
    boundary, so each is journaled — and a journaled call does not re-run."""

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


def _kernel(rundir: str, tally: str, recover: bool = False, tick: float = TICK):
    return Kernel(
        store=Store(rundir), tick=tick, recover=recover,
        tools={"sql": {"db": tally}}, permissions={"*": ["sql"]},
    )


# ============================ LangGraph =====================================

def _lg_billing(tally: str, per_node: bool):
    """Two shapes: everything in one node (the obvious way to write it), or
    one node per call (the decomposition that buys finer checkpoints)."""
    from typing import TypedDict

    from langgraph.graph import StateGraph, START, END

    class S(TypedDict):
        done: int

    g = StateGraph(S)
    if not per_node:
        def work(state: S) -> S:
            for i in range(CALLS):
                bill(tally, i, OP_DELAY)
            return {"done": CALLS}
        g.add_node("work", work)
        g.add_edge(START, "work")
        g.add_edge("work", END)
        return g

    def make(i: int):
        def work(state: S) -> S:
            bill(tally, i, OP_DELAY)
            return {"done": state.get("done", 0) + 1}
        return work

    prev = START
    for i in range(CALLS):
        g.add_node(f"c{i}", make(i))
        g.add_edge(prev, f"c{i}")
        prev = f"c{i}"
    g.add_edge(prev, END)
    return g


def _lg_steps(tally: str, steps: int, delay: float):
    from typing import TypedDict

    from langgraph.graph import StateGraph, START, END

    class S(TypedDict):
        n: int

    def make(i: int):
        def work(state: S) -> S:
            bill(tally, i, delay)
            return {"n": state.get("n", 0) + 1}
        return work

    g = StateGraph(S)
    prev = START
    for i in range(steps):
        g.add_node(f"s{i}", make(i))
        g.add_edge(prev, f"s{i}")
        prev = f"s{i}"
    g.add_edge(prev, END)
    return g


def _lg_run(graph, ckpt: str, thread: str, resume: bool) -> None:
    from langgraph.checkpoint.sqlite import SqliteSaver

    conn = sqlite3.connect(ckpt, check_same_thread=False)
    app = graph.compile(checkpointer=SqliteSaver(conn))
    # A chain of N nodes is N super-steps; the default ceiling is 25.
    cfg = {"configurable": {"thread_id": thread}, "recursion_limit": 4 * STEPS}
    app.invoke(None if resume else {"done": 0, "n": 0}, cfg)
    conn.close()


# ============================ CrewAI Flows ==================================

def _crewai_flow(tally: str, db: str, n: int, delay: float):
    """A persisted Flow, one method per call — CrewAI's own chaining idiom.

    Methods are generated because @start/@listen wire the chain at class
    definition time and we need n of them.
    """
    from pydantic import BaseModel
    from crewai.flow.flow import Flow, listen, start
    from crewai.flow.persistence import persist
    from crewai.flow.persistence.sqlite import SQLiteFlowPersistence

    class S(BaseModel):
        id: str = "agentos-compare-flow"
        done: int = 0

    def make(i: int):
        def step(self):
            bill(tally, i, delay)
            self.state.done = i + 1
        step.__name__ = f"c{i}"
        return step

    body, prev = {}, None
    for i in range(n):
        fn = make(i)
        body[f"c{i}"] = start()(fn) if prev is None else listen(prev)(fn)
        prev = body[f"c{i}"]
    cls = type("BillFlow", (Flow[S],), body)
    return persist(SQLiteFlowPersistence(db_path=db))(cls), S


# ============================ AutoGen =======================================

async def _autogen_run(tally: str, statefile: str, n: int, delay: float,
                       resume: bool) -> None:
    from benchmarks import _autogen_defs as ad

    await ad.run(tally, statefile, n, delay)


# ============================ child entry point =============================

def _child(kind: str, rundir: str, tally: str, resume: bool) -> int:
    ckpt = os.path.join(rundir, "ckpt.db")
    if kind == "agentos":
        k = _kernel(rundir, tally, recover=resume)
        if not resume:
            k.spawn(BillingAgent(calls=CALLS, delay=OP_DELAY))
        asyncio.run(k.run())
    elif kind in ("langgraph", "langgraph-nodes"):
        _lg_run(_lg_billing(tally, kind.endswith("nodes")), ckpt, "t", resume)
    elif kind == "crewai":
        flow_cls, state_cls = _crewai_flow(
            tally, os.path.join(rundir, "flow.db"), CALLS, OP_DELAY)
        f = flow_cls()
        with _quiet():
            if resume:
                f.kickoff(restore_from_state_id=state_cls().id)
            else:
                f.kickoff(inputs={"id": state_cls().id})
    elif kind == "autogen":
        asyncio.run(_autogen_run(
            tally, os.path.join(rundir, "state.json"), CALLS, OP_DELAY, resume))
    return 0


# ============================ 1. recovery ===================================

def bench_recovery(kind: str) -> dict:
    """Start the workload, kill it hard once KILL_AFTER calls have landed,
    resume it, and count how many billable calls ran more than once."""
    tmp = tempfile.mkdtemp()
    rundir = os.path.join(tmp, "run")
    os.makedirs(rundir, exist_ok=True)
    tally = os.path.join(tmp, "tally.db")
    tally_init(tally)

    env = dict(os.environ, PYTHONPATH=str(ROOT))
    args = [sys.executable, "-X", "utf8", os.path.abspath(__file__),
            "--child", kind, "--rundir", rundir, "--tally", tally]

    proc = subprocess.Popen(args, env=env, stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE)
    deadline = time.time() + 120
    while time.time() < deadline:
        if tally_count(tally) >= KILL_AFTER or proc.poll() is not None:
            break
        time.sleep(0.01)
    before = tally_count(tally)
    proc.kill()
    proc.wait(timeout=30)

    t0 = time.perf_counter()
    rec = subprocess.run(args + ["--resume"], env=env, capture_output=True,
                         timeout=300)
    recovery_s = time.perf_counter() - t0

    total = tally_count(tally)
    return {
        "before_kill": before,
        "total": total,
        "repeated": max(0, total - CALLS),
        "recovery_s": recovery_s,
        "ok": rec.returncode == 0 and total >= CALLS,
        "err": rec.stderr.decode("utf-8", "replace")[-200:],
    }


def bench_recovery_temporal() -> dict:
    """Temporal keeps durable state in a server, so the process that dies is
    the worker; a fresh worker picks the workflow up from its event history."""
    from temporalio.client import Client
    from temporalio.testing import WorkflowEnvironment
    from benchmarks._temporal_defs import TASK_QUEUE

    async def go() -> dict:
        tmp = tempfile.mkdtemp()
        tally = os.path.join(tmp, "tally.db")
        tally_init(tally)
        env = dict(os.environ, BENCH_TALLY=tally, PYTHONPATH=str(ROOT),
                   BENCH_DELAY=str(OP_DELAY))

        async with await WorkflowEnvironment.start_local() as wenv:
            addr = wenv.client.service_client.config.target_host
            env["BENCH_ADDR"] = addr
            client = await Client.connect(addr)
            handle = await client.start_workflow(
                "BillWorkflow", CALLS, id="cmp-bill", task_queue=TASK_QUEUE)

            worker = [sys.executable, "-X", "utf8",
                      os.path.abspath(__file__), "--temporal-worker"]
            w1 = subprocess.Popen(worker, env=env, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL)
            deadline = time.time() + 120
            while time.time() < deadline and tally_count(tally) < KILL_AFTER:
                if w1.poll() is not None:
                    break
                await asyncio.sleep(0.02)
            before = tally_count(tally)
            w1.kill(); w1.wait()

            t0 = time.perf_counter()
            w2 = subprocess.Popen(worker, env=env, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL)
            ok = True
            try:
                await asyncio.wait_for(handle.result(), timeout=180)
            except Exception:
                ok = False
            recovery_s = time.perf_counter() - t0
            w2.kill(); w2.wait()

            total = tally_count(tally)
            return {"before_kill": before, "total": total,
                    "repeated": max(0, total - CALLS),
                    "recovery_s": recovery_s, "ok": ok, "err": ""}

    return asyncio.run(go())


# ============================ 2. step overhead ==============================

async def steps_agentos(delay: float, n: int = STEPS, tick: float = TICK) -> float:
    tmp = tempfile.mkdtemp()
    tally = os.path.join(tmp, "tally.db")
    tally_init(tally)
    k = _kernel(os.path.join(tmp, "run"), tally, tick=tick)
    k.spawn(StepAgent(steps=n, delay=delay))
    t0 = time.perf_counter()
    await k.run()
    return time.perf_counter() - t0


def steps_langgraph(delay: float, n: int = STEPS) -> float:
    tmp = tempfile.mkdtemp()
    tally = os.path.join(tmp, "tally.db")
    tally_init(tally)
    t0 = time.perf_counter()
    _lg_run(_lg_steps(tally, n, delay), os.path.join(tmp, "c.db"), "s", False)
    return time.perf_counter() - t0


def steps_crewai(delay: float, n: int = STEPS) -> float:
    tmp = tempfile.mkdtemp()
    tally = os.path.join(tmp, "tally.db")
    tally_init(tally)
    flow_cls, _ = _crewai_flow(tally, os.path.join(tmp, "f.db"), n, delay)
    with _quiet():
        t0 = time.perf_counter()
        flow_cls().kickoff()
        return time.perf_counter() - t0


def steps_autogen(delay: float, n: int = STEPS) -> float:
    tmp = tempfile.mkdtemp()
    tally = os.path.join(tmp, "tally.db")
    tally_init(tally)
    t0 = time.perf_counter()
    asyncio.run(_autogen_run(tally, os.path.join(tmp, "s.json"),
                             n, delay, False))
    return time.perf_counter() - t0


def steps_temporal(delay: float) -> float:
    from temporalio.client import Client
    from temporalio.worker import Worker
    from benchmarks import _temporal_defs as td

    async def go() -> float:
        tmp = tempfile.mkdtemp()
        tally = os.path.join(tmp, "tally.db")
        tally_init(tally)
        os.environ["BENCH_TALLY"] = tally
        os.environ["BENCH_DELAY"] = str(delay)
        from temporalio.testing import WorkflowEnvironment
        async with await WorkflowEnvironment.start_local() as wenv:
            client = await Client.connect(
                wenv.client.service_client.config.target_host)
            async with Worker(client, task_queue=td.TASK_QUEUE,
                              workflows=[td.StepWorkflow],
                              activities=[td.durable_step]):
                t0 = time.perf_counter()
                await client.execute_workflow(
                    "StepWorkflow", STEPS, id="cmp-steps",
                    task_queue=td.TASK_QUEUE)
                return time.perf_counter() - t0

    return asyncio.run(go())


# ============================ 3. human in the loop ==========================

async def hitl_agentos(rounds: int = 5) -> tuple[float, float]:
    from agentos.kernel.states import AgentState
    lat = []
    for _ in range(rounds):
        tmp = tempfile.mkdtemp()
        tally = os.path.join(tmp, "tally.db")
        tally_init(tally)
        k = _kernel(os.path.join(tmp, "run"), tally)
        pid = k.spawn(GatedAgent())
        run = asyncio.create_task(k.run())
        while k.table.get(pid).state is not AgentState.BLOCKED:
            await asyncio.sleep(0.002)
        t0 = time.perf_counter()
        k.approve(pid, "Operator")
        await run
        lat.append((time.perf_counter() - t0) * 1000)
    return statistics.median(lat), max(lat)


def hitl_langgraph(rounds: int = 5) -> tuple[float, float]:
    from typing import TypedDict

    from langgraph.checkpoint.sqlite import SqliteSaver
    from langgraph.graph import StateGraph, START, END
    from langgraph.types import Command, interrupt

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
        conn = sqlite3.connect(os.path.join(tmp, "c.db"), check_same_thread=False)
        app = g.compile(checkpointer=SqliteSaver(conn))
        cfg = {"configurable": {"thread_id": f"t{i}"}}
        app.invoke({"v": ""}, cfg)
        t0 = time.perf_counter()
        app.invoke(Command(resume="ok"), cfg)
        lat.append((time.perf_counter() - t0) * 1000)
        conn.close()
    return statistics.median(lat), max(lat)


def hitl_temporal(rounds: int = 3) -> tuple[float, float]:
    from temporalio.client import Client
    from temporalio.worker import Worker
    from temporalio.testing import WorkflowEnvironment
    from benchmarks import _temporal_defs as td

    async def go() -> tuple[float, float]:
        async with await WorkflowEnvironment.start_local() as wenv:
            client = await Client.connect(
                wenv.client.service_client.config.target_host)
            async with Worker(client, task_queue=td.TASK_QUEUE,
                              workflows=[td.GatedWorkflow], activities=[]):
                lat = []
                for i in range(rounds):
                    h = await client.start_workflow(
                        "GatedWorkflow", id=f"cmp-gate-{i}",
                        task_queue=td.TASK_QUEUE)
                    await asyncio.sleep(0.3)  # let it reach the wait
                    t0 = time.perf_counter()
                    await h.signal("approve")
                    await h.result()
                    lat.append((time.perf_counter() - t0) * 1000)
                return statistics.median(lat), max(lat)

    return asyncio.run(go())


# ============================ the table =====================================

def _row(label, *cells) -> None:
    print(f"   {label:<24}" + "".join(f"{c:>16}" for c in cells))


def main() -> int:
    present = [k for k, v in HAVE.items() if v]
    print("AgentOS vs the field — identical workloads, real kills, shared tally")
    print(f"comparators present: {', '.join(present) if present else 'none'}")
    missing = [k for k, v in HAVE.items() if not v]
    if missing:
        print(f"skipped (not installed): {', '.join(missing)}")
    print()

    # Temporal runs last within every section: its server is a live process
    # that would otherwise be competing for CPU during everyone else's timings.
    # -- 1. recovery
    print(f"1. RECOVERY — billable calls REPEATED after a hard kill"
          f" ({CALLS} calls, killed after {KILL_AFTER})")
    _row("framework", "ran before", "total ran", "repeated", "recovery")
    plan = [("agentos", "agentos", True)]
    if HAVE["langgraph"]:
        plan += [("langgraph", "langgraph", True),
                 ("langgraph (per-node)", "langgraph-nodes", True)]
    if HAVE["crewai"]:
        plan += [("crewai (flow)", "crewai", True)]
    if HAVE["autogen"]:
        plan += [("autogen", "autogen", True)]
    for label, kind, _ in plan:
        r = bench_recovery(kind)
        note = "" if r["ok"] else "  !!"
        _row(label, r["before_kill"], r["total"], r["repeated"],
             f"{r['recovery_s']:.2f}s" + note)
    if HAVE["temporal"]:
        r = bench_recovery_temporal()
        _row("temporal", r["before_kill"], r["total"], r["repeated"],
             f"{r['recovery_s']:.2f}s" + ("" if r["ok"] else "  !!"))
    print(f"\n   repeated = calls that executed twice: work redone, and in a"
          f" real system\n   model spend paid twice. Lower is better.\n")

    # -- 2. overhead. Warm each framework first: a one-step run pays the
    # import and any first-compile cost so it lands outside the timers.
    print(f"2. OVERHEAD — one durable step, no real work ({STEPS} steps)")
    asyncio.run(steps_agentos(0.0, n=1))
    if HAVE["langgraph"]:
        steps_langgraph(0.0, n=1)
    if HAVE["crewai"]:
        steps_crewai(0.0, n=1)
    if HAVE["autogen"]:
        steps_autogen(0.0, n=1)
    _row("framework", "wall", "per step")
    a = asyncio.run(steps_agentos(0.0))
    _row("agentos", f"{a:.2f}s", f"{a/STEPS*1000:.1f}ms")
    if HAVE["langgraph"]:
        v = steps_langgraph(0.0)
        _row("langgraph", f"{v:.2f}s", f"{v/STEPS*1000:.1f}ms")
    if HAVE["crewai"]:
        v = steps_crewai(0.0)
        _row("crewai (flow)", f"{v:.2f}s", f"{v/STEPS*1000:.1f}ms")
    if HAVE["autogen"]:
        v = steps_autogen(0.0)
        _row("autogen", f"{v:.2f}s", f"{v/STEPS*1000:.1f}ms")
    if HAVE["temporal"]:
        v = steps_temporal(0.0)
        _row("temporal", f"{v:.2f}s", f"{v/STEPS*1000:.1f}ms")

    # -- 2b. the same, with realistic work per step
    print(f"\n2b. the same {STEPS} steps with {int(REAL_WORK*1000)}ms of real"
          f" work each\n    (one modest model call) — overhead as a share of"
          f" the total")
    _row("framework", "wall", "vs floor")
    floor = STEPS * REAL_WORK
    ar = asyncio.run(steps_agentos(REAL_WORK))
    _row("agentos", f"{ar:.2f}s", f"+{(ar-floor)/floor*100:.1f}%")
    if HAVE["langgraph"]:
        v = steps_langgraph(REAL_WORK)
        _row("langgraph", f"{v:.2f}s", f"+{(v-floor)/floor*100:.1f}%")
    if HAVE["crewai"]:
        v = steps_crewai(REAL_WORK)
        _row("crewai (flow)", f"{v:.2f}s", f"+{(v-floor)/floor*100:.1f}%")
    if HAVE["autogen"]:
        v = steps_autogen(REAL_WORK)
        _row("autogen", f"{v:.2f}s", f"+{(v-floor)/floor*100:.1f}%")
    if HAVE["temporal"]:
        v = steps_temporal(REAL_WORK)
        _row("temporal", f"{v:.2f}s", f"+{(v-floor)/floor*100:.1f}%")

    # -- 3. human in the loop
    print("\n3. HUMAN-IN-THE-LOOP — approve -> the agent has finished")
    _row("framework", "median", "worst")
    m, w = asyncio.run(hitl_agentos())
    _row("agentos", f"{m:.1f}ms", f"{w:.1f}ms")
    if HAVE["langgraph"]:
        m, w = hitl_langgraph()
        _row("langgraph", f"{m:.1f}ms", f"{w:.1f}ms")
    if HAVE["temporal"]:
        m, w = hitl_temporal()
        _row("temporal", f"{m:.1f}ms", f"{w:.1f}ms")
    print("   crewai / autogen: no durable wait-for-a-human primitive to time")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--child")
    ap.add_argument("--rundir")
    ap.add_argument("--tally")
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--temporal-worker", action="store_true")
    ns = ap.parse_args()
    if ns.temporal_worker:
        from temporalio.client import Client
        from temporalio.worker import Worker
        from benchmarks import _temporal_defs as td

        async def _serve() -> None:
            client = await Client.connect(os.environ["BENCH_ADDR"])
            async with Worker(client, task_queue=td.TASK_QUEUE,
                              workflows=[td.BillWorkflow, td.StepWorkflow],
                              activities=[td.billable, td.durable_step]):
                while True:
                    await asyncio.sleep(3600)

        raise SystemExit(asyncio.run(_serve()))
    if ns.child:
        raise SystemExit(_child(ns.child, ns.rundir, ns.tally, ns.resume))
    raise SystemExit(main())
