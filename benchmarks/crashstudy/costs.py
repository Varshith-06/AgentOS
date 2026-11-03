"""What each configuration costs when nothing crashes.

    python -m benchmarks.crashstudy.costs --runs 10 --out results/v2/costs.jsonl

The crash runs say what a crash re-bills. This says what it costs to be ready
for one: time added to each call, time to the first call, memory and CPU across
the whole process tree. The stand-in's latency and the think time are known, so
whatever a cycle takes beyond them is the configuration's own.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import psutil

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from benchmarks.crashstudy import paidapi  # noqa: E402
from benchmarks.crashstudy.run import (NEEDS_TEMPORAL, drain, kill_tree, popen,  # noqa: E402
                                       start_gateway, start_temporal)

FRAMEWORKS = ["agentos", "langgraph-node", "langgraph-loop", "temporal", "temporal-split",
              "dbos", "crewai", "crewai-guarded", "crewai-checkpoint", "prefect", "autogen"]


def arms(frameworks: list[str]) -> list[dict]:
    """Every framework as it is, then AgentOS's mechanisms, the gateway beside
    no gateway, and side-by-side agents with and without couriers."""
    out = [{"arm": fw, "fw": fw} for fw in frameworks]
    if "agentos" not in frameworks:
        return out
    out += [{"arm": "agentos+courier", "fw": "agentos", "courier": True},
            {"arm": "agentos+durable", "fw": "agentos", "durable": True},
            {"arm": "agentos+courier+durable", "fw": "agentos", "courier": True, "durable": True},
            {"arm": "agentos+noadopt", "fw": "agentos", "adopt": False}]
    out += [{"arm": f"{fw}+gateway", "fw": fw, "gateway": True, "keyed": True}
            for fw in ("agentos", "langgraph-node", "dbos") if fw in frameworks]
    out += [{"arm": f"{fw}+key", "fw": fw, "keyed": True}
            for fw in ("langgraph-node", "dbos") if fw in frameworks]
    for n in (4, 8, 16):
        out.append({"arm": f"agentos x{n}", "fw": "agentos", "agents": n})
        out.append({"arm": f"agentos+courier x{n}", "fw": "agentos", "courier": True, "agents": n})
    return out


def sample(proc: psutil.Process, cpu: dict[int, float]) -> int:
    """The tree's memory now; CPU is kept per process, as it dies with them."""
    rss = 0
    try:
        tree = [proc] + proc.children(recursive=True)
    except psutil.Error:
        return 0
    for p in tree:
        try:
            with p.oneshot():
                rss += p.memory_info().rss
                t = p.cpu_times()
                cpu[p.pid] = t.user + t.system
        except psutil.Error:
            pass
    return rss


def one(arm: dict, args, api: str, ledger: str, temporal: str, scratch: Path, n: int) -> dict:
    run = f"{arm['arm'].replace(' ', '')}-{n}-{random.randrange(16**8):08x}"
    d = scratch / run
    d.mkdir(parents=True)
    agents = arm.get("agents", 1)
    env = dict(os.environ, PYTHONPATH=str(ROOT), PYTHONUTF8="1", CS_RUN=run, CS_PORT=api,
               CS_DIR=str(d), CS_CALLS=str(args.calls), CS_THINK=str(args.think),
               CS_KEYED="1" if arm.get("keyed") else "0", CS_TEMPORAL=temporal,
               PREFECT_HOME=str(scratch / "prefect"), PREFECT_LOGGING_LEVEL="WARNING",
               PREFECT_LOCAL_STORAGE_PATH=str(scratch / "prefect" / "storage"),
               CS_ADOPT="1" if arm.get("adopt", True) else "0",
               CS_COURIER="1" if arm.get("courier") else "0", CS_RETRIES="0",
               CS_AGENTS=str(agents))
    if arm.get("durable"):
        env["CS_DURABLE"] = "1"
    t0 = time.time()
    p = popen([args.python, "-m", "benchmarks.crashstudy.adapters", arm["fw"], "start"],
              env, d / "start.log", breakaway=bool(arm.get("courier")))
    watched, cpu, peak = psutil.Process(p.pid), {}, 0
    while p.poll() is None and time.time() - t0 < args.timeout:
        peak = max(peak, sample(watched, cpu))
        time.sleep(0.02)
    wall = time.time() - t0
    if p.poll() is None:
        kill_tree(p)
    else:
        kill_tree(p)
    time.sleep(0.3 if arm.get("courier") else 0)
    charges = paidapi.charges(ledger, run)
    import sqlite3
    conn = sqlite3.connect(ledger, timeout=10)
    rows = conn.execute("SELECT i, ts FROM charges WHERE run = ? ORDER BY id", (run,)).fetchall()
    conn.close()
    per: dict[int, list[float]] = {}
    for i, ts in rows:
        per.setdefault(i // args.calls, []).append(ts)
    gaps = [b - a for ts in per.values() for a, b in zip(ts, ts[1:])]
    return {"arm": arm["arm"], "n": n, "agents": agents, "calls": len(charges),
            "expected": args.calls * agents, "rc": p.returncode, "wall": wall,
            "startup": rows[0][1] - t0 if rows else None,
            "cycle": statistics.median(gaps) if gaps else None, "gaps": gaps,
            "overhead_ms": 1000 * (statistics.median(gaps) - args.think - args.latency) if gaps else None,
            "peak_rss_mb": peak / 2**20, "cpu_s": sum(cpu.values()),
            "latency": args.latency, "think": args.think, "platform": sys.platform,
            "err": (d / "start.log").read_bytes()[-300:].decode("utf-8", "replace")
            if p.returncode else ""}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--runs", type=int, default=10)
    ap.add_argument("--calls", type=int, default=6)
    ap.add_argument("--latency", type=float, default=0.3)
    ap.add_argument("--think", type=float, default=0.1)
    ap.add_argument("--timeout", type=float, default=120)
    ap.add_argument("--frameworks", default=",".join(FRAMEWORKS))
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    frameworks = args.frameworks.split(",")
    scratch = Path(tempfile.mkdtemp(prefix="crashstudy-costs-"))
    ledger = str(scratch / "ledger.db")
    paidapi.init(ledger)
    api = subprocess.Popen(
        [sys.executable, "-m", "benchmarks.crashstudy.paidapi", "--ledger", ledger,
         "--latency", str(args.latency), "--ignore-keys"],
        cwd=ROOT, stdout=subprocess.PIPE, env=dict(os.environ, PYTHONPATH=str(ROOT)))
    port = api.stdout.readline().decode().strip()
    drain(api.stdout)
    gateway, gw_port = None, None
    if any(a.get("gateway") for a in arms(frameworks)):
        gateway, url = start_gateway(f"http://127.0.0.1:{port}", scratch / "gateway.db")
        gw_port = url.rsplit(":", 1)[1]
    temporal_proc, temporal = None, ""
    if NEEDS_TEMPORAL & set(frameworks):
        temporal_proc, temporal = start_temporal(args.python)
    done: dict[str, int] = {}
    if os.path.exists(args.out):
        for line in open(args.out, encoding="utf-8"):
            r = json.loads(line)
            done[r["arm"]] = done.get(r["arm"], 0) + 1
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(args.out, "a", encoding="utf-8") as out:
            # Round by round, so drift in the machine's load spreads over every arm.
            for n in range(args.runs):
                for arm in arms(frameworks):
                    if n < done.get(arm["arm"], 0):
                        continue
                    rec = one(arm, args, gw_port if arm.get("gateway") else port, ledger,
                              temporal, scratch, n)
                    out.write(json.dumps(rec) + "\n")
                    out.flush()
                    print(f"{n} {arm['arm']:26} rc={rec['rc']} calls={rec['calls']}/{rec['expected']} "
                          f"over={rec['overhead_ms'] or 0:6.1f}ms start={rec['startup'] or 0:5.2f}s "
                          f"rss={rec['peak_rss_mb']:6.0f}MB cpu={rec['cpu_s']:5.2f}s", flush=True)
    finally:
        api.kill()
        if gateway is not None:
            gateway.kill()
        if temporal_proc is not None:
            kill_tree(temporal_proc)
    return 0


if __name__ == "__main__":
    sys.exit(main())
