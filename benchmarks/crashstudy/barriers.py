"""Kills at exact steps of the recovery protocol, not at random moments.

    python3 -m benchmarks.crashstudy.barriers --trials 15 --out results/v2/barriers

Random kills seldom land between two steps of the protocol. Here the run kills
itself at a named step of a chosen call: hooks in the harness (client.barrier,
installed by adapters.agentos), with nothing added to AgentOS. Each row of STEPS
says which step, what dies, and what the design says should follow: duplicates
and flags. POSIX only: it needs a process group to kill.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import subprocess
import sys
import tempfile
import time
import types
import uuid
from pathlib import Path

from benchmarks.crashstudy import paidapi
from benchmarks.crashstudy.run import ROOT, drain, popen, replies, run_env, signals, tail

# step, what dies, couriers, adoption, expected duplicate, expected flag
STEPS = [
    # Couriers on, the runtime dies: the courier carries on, or the call never left.
    ("intent-written", "runtime", "on", "yes", 0, 0),
    ("dispatched", "runtime", "on", "yes", 0, 0),
    ("call-starting", "runtime", "on", "yes", 0, 0),
    ("reply-received", "runtime", "on", "yes", 0, 0),
    ("before-journal", "runtime", "on", "yes", 0, 0),
    ("journaled", "runtime", "on", "yes", 0, 0),
    # The courier dies too: the outcome is unknown, so the call is sent again and flagged.
    ("call-starting", "courier", "on", "yes", 0, 1),
    ("reply-received", "courier", "on", "yes", 1, 1),
    ("call-starting", "both", "on", "yes", 0, 1),
    ("reply-received", "both", "on", "yes", 1, 1),
    # No couriers, the runtime dies: the baseline.
    ("intent-written", "runtime", "off", "yes", 0, 1),
    ("call-starting", "runtime", "off", "yes", 0, 1),
    ("reply-received", "runtime", "off", "yes", 1, 1),
    ("before-journal", "runtime", "off", "yes", 1, 1),
    ("journaled", "runtime", "off", "yes", 0, 0),
    # The agent dies, the kernel lives: adoption.
    ("intent-written", "agent", "off", "yes", 0, 0),
    ("call-starting", "agent", "off", "yes", 0, 0),
    ("reply-received", "agent", "off", "yes", 0, 0),
    ("before-journal", "agent", "off", "yes", 0, 0),
    ("journaled", "agent", "off", "yes", 0, 0),
    # The control: adoption off, so a kill before the reply repeats the call. A kill as the
    # kernel takes the reply does not: the kernel journals it before it sees the agent die.
    ("call-starting", "agent", "off", "no", 1, 1),
    ("reply-received", "agent", "off", "no", 0, 0),
]


def run_one(step, kill, courier, adopt, at, args, port, ledger, scratch, n) -> dict:
    run = f"b{n}-{uuid.uuid4().hex[:8]}"
    rundir = scratch / run
    rundir.mkdir(parents=True)
    ns = types.SimpleNamespace(calls=args.calls, think=args.think, adopt=adopt, courier=courier,
                               agents=1, model_path=False, drift=None, no_fingerprint=False,
                               durable=False)
    env = run_env("agentos", run, rundir, ns, port, None, scratch, False,
                  3 if kill in ("agent", "courier") else 0)
    env.update(CS_BARRIER=step, CS_BARRIER_KILL=kill, CS_BARRIER_AT=str(at))
    cmd = [args.python, "-m", "benchmarks.crashstudy.adapters", "agentos"]
    rec = {"step": step, "kill": kill, "courier": courier, "adopt": adopt == "yes", "at": at,
           "run": run, "calls": args.calls, "latency": args.latency, "think": args.think}
    p = popen(cmd + ["start"], env, rundir / "start.log")
    try:
        rc = p.wait(timeout=args.timeout)
    except subprocess.TimeoutExpired:
        p.kill()
        rec.update(status="start-timeout", err=tail(rundir / "start.log"))
        return rec
    fired = (rundir / "barrier.fired").exists()
    if kill in ("runtime", "both"):
        time.sleep(args.pause)
        try:
            res = subprocess.run(cmd + ["resume"], env=env, capture_output=True, timeout=args.timeout)
            rc, err = res.returncode, res.stderr.decode("utf-8", "replace")[-400:]
        except subprocess.TimeoutExpired:
            rc, err = "timeout", ""
    else:
        err = tail(rundir / "start.log", 400) if rc else ""
    time.sleep(args.settle)  # a courier still at work when the run ended
    idx = paidapi.charges(ledger, run)
    rec.update(status=("ok" if rc == 0 else "failed") if fired else "not-fired", rc=rc, err=err if rc else "",
               signals=signals(rundir), charged=len(idx), duplicates=len(idx) - len(set(idx)),
               missing=args.calls - len(set(idx)), **replies(rundir))
    return rec


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--trials", type=int, default=15)
    ap.add_argument("--calls", type=int, default=6)
    ap.add_argument("--latency", type=float, default=0.3)
    ap.add_argument("--think", type=float, default=0.1)
    ap.add_argument("--pause", type=float, default=0.2)
    ap.add_argument("--settle", type=float, default=1.0)
    ap.add_argument("--timeout", type=float, default=120)
    ap.add_argument("--seed", type=int, default=601)
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    rng = random.Random(args.seed)
    scratch = Path(tempfile.mkdtemp(prefix="crashstudy-barriers-"))
    ledger = str(out / "ledger.db")
    paidapi.init(ledger)
    api = subprocess.Popen(
        [args.python, "-m", "benchmarks.crashstudy.paidapi", "--ledger", ledger,
         "--latency", str(args.latency), "--ignore-keys"], cwd=ROOT, stdout=subprocess.PIPE,
        env={**os.environ, "PYTHONPATH": str(ROOT)})
    port = int(api.stdout.readline())
    drain(api.stdout)
    try:
        for step, kill, courier, adopt, want_dup, want_flag in STEPS:
            name = f"{step}--{kill}--courier-{courier}--adopt-{adopt}"
            # Interrupted runs resume: trials already on file count, and a line cut
            # short by the interruption is dropped.
            kept, tries = [], 0
            path = out / f"{name}.jsonl"
            if path.exists():
                for line in path.read_text(encoding="utf-8").splitlines():
                    try:
                        old = json.loads(line)
                    except ValueError:
                        continue
                    if old.get("status") in ("ok", "failed"):
                        kept.append(old)
            with open(path, "a", encoding="utf-8") as fh:
                while len(kept) < args.trials and tries < args.trials * 2:
                    tries += 1
                    rec = run_one(step, kill, courier, adopt, rng.randint(1, args.calls), args, port,
                                  ledger, scratch, tries)
                    rec.update(want_dup=want_dup, want_flag=want_flag)
                    fh.write(json.dumps(rec) + "\n")
                    fh.flush()
                    if rec["status"] in ("ok", "failed"):
                        kept.append(rec)
            dup = sum(r["duplicates"] > 0 for r in kept)
            flag = sum(r["signals"].get("announced", 0) > 0 for r in kept)
            miss = sum(r["missing"] > 0 or r["answered"] < r["calls"] for r in kept)
            wrong = sum(r["wrong_replies"] > 0 for r in kept)
            failed = sum(r["status"] == "failed" for r in kept)
            print(f"{step:16} {kill:8} courier={courier:3} adopt={adopt:3} n={len(kept):2} "
                  f"dup={dup:2} (want {want_dup}) flag={flag:2} (want {want_flag}) "
                  f"missing={miss} wrong={wrong} failed={failed} tries={tries}", flush=True)
    finally:
        api.kill()
    return 0


if __name__ == "__main__":
    sys.exit(main())
