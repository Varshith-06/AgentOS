"""Kill each framework at a random moment, recover it its own way, count the bill.

    python -m benchmarks.crashstudy.run --trials 30 --out results.jsonl

Per trial: start the workload; once the provider has charged its first call,
pick a kill time uniformly over the rest of the work and kill the whole process
tree; then run the framework's documented resume to completion. The ledger,
written by the provider and nobody else, says what was charged.

Nothing here asserts. A trial that never reaches its first charge, finishes
before its kill time, or fails to resume is recorded as such, not dropped.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import signal
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from benchmarks.crashstudy import paidapi  # noqa: E402

NEEDS_TEMPORAL = {"temporal"}
# A kill this far past its drawn time is a host stall, not a sample (seconds).
LATE = 0.05


if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.CreateJobObjectW.restype = wintypes.HANDLE
    _k32.OpenProcess.restype = wintypes.HANDLE
    _JOBS: dict[int, int] = {}

    def _jail(proc: subprocess.Popen) -> None:
        """Put the child in a job object: everything it spawns joins it."""
        job = _k32.CreateJobObjectW(None, None)
        handle = _k32.OpenProcess(0x1F0FFF, False, proc.pid)  # PROCESS_ALL_ACCESS
        if not _k32.AssignProcessToJobObject(job, handle):
            raise OSError(ctypes.get_last_error(), "AssignProcessToJobObject")
        _k32.CloseHandle(handle)
        _JOBS[proc.pid] = job


def kill_tree(proc: subprocess.Popen) -> float:
    """Kill the process and all its descendants at once; return when it landed."""
    if os.name == "nt":
        job = _JOBS.pop(proc.pid, None)
        at = time.time()
        if job is not None:
            _k32.TerminateJobObject(job, 1)
            _k32.CloseHandle(job)
        elif proc.poll() is None:
            proc.kill()
    else:
        at = time.time()
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    proc.wait(timeout=30)
    return at


def popen(args, env, log: Path) -> subprocess.Popen:
    kw = {}
    if os.name != "nt":
        kw["start_new_session"] = True
    # A file, not a pipe: nobody reads a pipe while the child runs, and a chatty
    # framework would block on a full one and never get to its first call.
    with open(log, "wb") as fh:
        proc = subprocess.Popen(args, env=env, stdout=subprocess.DEVNULL,
                                stderr=fh, **kw)
    if os.name == "nt":
        _jail(proc)
    return proc


def drain(stream) -> None:
    """Keep reading a server's stdout so a full pipe never stalls it."""
    threading.Thread(target=lambda: [None for _ in stream], daemon=True).start()


def tail(path: Path, n: int = 800) -> str:
    try:
        return path.read_bytes()[-n:].decode("utf-8", "replace")
    except OSError:
        return ""


def first_charge(ledger: str, run: str) -> float | None:
    import sqlite3
    conn = sqlite3.connect(ledger, timeout=10)
    try:
        row = conn.execute(
            "SELECT MIN(ts) FROM charges WHERE run = ?", (run,)).fetchone()
        return row[0]
    finally:
        conn.close()


def charge_times(ledger: str, run: str) -> list[float]:
    import sqlite3
    conn = sqlite3.connect(ledger, timeout=10)
    try:
        return [r[0] for r in conn.execute(
            "SELECT ts FROM charges WHERE run = ? ORDER BY id", (run,))]
    finally:
        conn.close()


def signals(rundir: Path) -> dict:
    """What the framework told the application about re-execution, kept with
    the result so the analysis doesn't need the run's scratch directory.

    `attempts`: [call, attempt] per execution, where the framework exposes an
    attempt number to the call (Temporal, DBOS). `announced`: AgentOS's
    OutcomeAmbiguous events.
    """
    out: dict = {}
    notes = rundir / "notes.jsonl"
    if notes.exists():
        out["attempts"] = [[n["i"], n.get("attempt")] for n in
                           map(json.loads, notes.read_text(encoding="utf-8").splitlines())]
    db = rundir / "agentos" / "runtime.db"
    if db.exists():
        conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
        out["announced"] = conn.execute(
            "SELECT COUNT(*) FROM events WHERE type = 'OutcomeAmbiguous'").fetchone()[0]
        conn.close()
    return out


def trial(fw: str, n: int, args, port: int, ledger: str, temporal: str | None,
          rng: random.Random, keyed: bool, scratch: Path) -> dict:
    run = f"{fw}-{'k' if keyed else 'u'}-{n}-{rng.randrange(16**8):08x}"
    rundir = scratch / run
    rundir.mkdir(parents=True)
    env = dict(
        os.environ, PYTHONPATH=str(ROOT), PYTHONUTF8="1",
        CS_RUN=run, CS_PORT=str(port), CS_DIR=str(rundir),
        CS_CALLS=str(args.calls), CS_THINK=str(args.think),
        CS_KEYED="1" if keyed else "0", CS_TEMPORAL=temporal or "",
        PREFECT_HOME=str(scratch / "prefect"),
        PREFECT_LOGGING_LEVEL="WARNING",
        PREFECT_LOCAL_STORAGE_PATH=str(scratch / "prefect" / "storage"),
    )
    cmd = [args.python, "-m", "benchmarks.crashstudy.adapters", fw]
    # Work after the first charge: that call's latency, then (think + call)
    # for each of the rest. The kill lands uniformly somewhere in it.
    horizon = args.latency + (args.calls - 1) * (args.think + args.latency)
    offset = rng.uniform(0, horizon)

    rec = {"framework": fw, "keyed": keyed, "trial": n, "run": run,
           "calls": args.calls, "latency": args.latency, "think": args.think,
           "offset": offset, "horizon": horizon}
    log = rundir / "start.log"
    p = popen(cmd + ["start"], env, log)
    deadline = time.time() + args.timeout
    t0 = None
    while time.time() < deadline and p.poll() is None:
        t0 = first_charge(ledger, run)
        if t0 is not None:
            break
        time.sleep(0.005)
    if t0 is None:
        kill_tree(p)
        rec.update(status="never-started", err=tail(log))
        return rec
    kill_at = t0 + offset
    while time.time() < kill_at and p.poll() is None:
        time.sleep(0.002)
    if p.poll() is not None:
        rec.update(status="finished-before-kill", rc=p.returncode, err=tail(log))
        return rec
    killed = kill_tree(p)
    before = len(charge_times(ledger, run))

    time.sleep(args.pause)
    r0 = time.perf_counter()
    try:
        res = subprocess.run(cmd + ["resume"], env=env, capture_output=True,
                             timeout=args.timeout)
        rc, err = res.returncode, res.stderr.decode("utf-8", "replace")[-600:]
    except subprocess.TimeoutExpired:
        rc, err = "timeout", ""
    recovery = time.perf_counter() - r0

    times = charge_times(ledger, run)
    idx = paidapi.charges(ledger, run)
    rec["signals"] = signals(rundir)
    in_call = any(t <= killed < t + args.latency for t in times[:before])
    status = "ok" if rc == 0 and set(idx) == set(range(args.calls)) else "failed"
    if killed - kill_at > LATE:
        # The harness and the run froze together (a suspend, or the process tree
        # being paused) and the kill landed off its draw. Whatever happened next
        # is the stall's doing, not the crash's: a frozen call can outlive its
        # own timeout.
        status = "late"
    rec.update(
        status=status,
        rc=rc, err=err if rc != 0 else "",
        killed_after=killed - t0, charged_before_kill=before,
        charged=len(idx), duplicates=len(idx) - len(set(idx)),
        missing=args.calls - len(set(idx)), kill_in_call=in_call,
        recovery_s=recovery,
    )
    return rec


def summarise(rows: list[dict]) -> None:
    groups: dict[tuple, list[dict]] = {}
    for r in rows:
        groups.setdefault((r["framework"], r["keyed"]), []).append(r)
    print(f"\n{'framework':16} {'key':4} {'n':>3} {'ok':>3} {'P(dup)':>7} "
          f"{'E[dup]':>7} {'max':>4} {'in-call':>8} {'recov s':>8}  other")
    for (fw, k), rs in sorted(groups.items()):
        ok = [r for r in rs if r["status"] == "ok"]
        dups = [r["duplicates"] for r in ok]
        other = {}
        for r in rs:
            if r["status"] != "ok":
                other[r["status"]] = other.get(r["status"], 0) + 1
        print(
            f"{fw:16} {'yes' if k else 'no':4} {len(rs):>3} {len(ok):>3} "
            f"{(sum(d > 0 for d in dups) / len(dups)) if dups else float('nan'):>7.2f} "
            f"{statistics.mean(dups) if dups else float('nan'):>7.2f} "
            f"{max(dups) if dups else 0:>4} "
            f"{sum(r['kill_in_call'] for r in ok):>8} "
            f"{statistics.median([r['recovery_s'] for r in ok]) if ok else float('nan'):>8.2f}  "
            f"{other or ''}"
        )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--frameworks", default="agentos,langgraph-node,langgraph-loop,"
                    "temporal,dbos,crewai,crewai-guarded,crewai-checkpoint,prefect,autogen")
    ap.add_argument("--keyed", choices=["no", "yes", "both"], default="both")
    ap.add_argument("--trials", type=int, default=30)
    ap.add_argument("--calls", type=int, default=6)
    ap.add_argument("--latency", type=float, default=0.3)
    ap.add_argument("--think", type=float, default=0.1)
    ap.add_argument("--pause", type=float, default=0.2)
    ap.add_argument("--timeout", type=float, default=180)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("--out", default="crashstudy.jsonl")
    ap.add_argument("--summary", action="store_true",
                    help="only summarise an existing --out file")
    args = ap.parse_args()

    if args.summary:
        summarise([json.loads(l) for l in open(args.out, encoding="utf-8")])
        return 0

    fws = args.frameworks.split(",")
    keyed = {"no": [False], "yes": [True], "both": [False, True]}[args.keyed]
    # Picking up a run that was stopped: trials already on record are skipped,
    # and a fresh seed keeps the new kill times from repeating the old ones.
    done: dict[tuple, int] = {}
    if os.path.exists(args.out):
        for line in open(args.out, encoding="utf-8"):
            r = json.loads(line)
            if r["status"] == "late":
                continue  # drawn again
            done[(r["framework"], r["keyed"])] = done.get((r["framework"], r["keyed"]), 0) + 1
    rng = random.Random(args.seed + sum(done.values()))
    scratch = Path(tempfile.mkdtemp(prefix="crashstudy-"))
    # The ledger lives beside the results, not in scratch: a run killed from
    # outside must not take the only record of what was charged with it.
    stamp = time.strftime("%Y%m%d-%H%M%S")
    ledger = str(Path(args.out).with_name(f"{Path(args.out).stem}-{stamp}-ledger.db"))
    paidapi.init(ledger)
    api = subprocess.Popen(
        [sys.executable, "-m", "benchmarks.crashstudy.paidapi", "--ledger", ledger,
         "--latency", str(args.latency)],
        cwd=ROOT, stdout=subprocess.PIPE, env=dict(os.environ, PYTHONPATH=str(ROOT)))
    port = int(api.stdout.readline())
    drain(api.stdout)
    temporal_proc, temporal = None, None
    if NEEDS_TEMPORAL & set(fws):
        temporal_proc = subprocess.Popen(
            [args.python, "-m", "benchmarks.crashstudy.temporal_server"],
            cwd=ROOT, stdout=subprocess.PIPE, env=dict(os.environ, PYTHONPATH=str(ROOT)))
        if os.name == "nt":
            _jail(temporal_proc)  # it spawns the server binary; take both down
        # The dev server shares our stdout and prints a banner first.
        while True:
            raw = temporal_proc.stdout.readline()
            if not raw:
                raise RuntimeError("temporal dev server exited before its address")
            line = raw.decode(errors="replace").strip()
            if re.fullmatch(r"[\d.]+:\d+", line):
                temporal = line
                break
        drain(temporal_proc.stdout)

    rows = []
    try:
        with open(args.out, "a", encoding="utf-8") as out:
            # Interleaved, so drift in the machine's load spreads over everyone.
            plan = [(fw, k, n) for n in range(args.trials) for fw in fws for k in keyed
                    if n >= done.get((fw, k), 0)]
            for i, (fw, k, n) in enumerate(plan, 1):
                rec = trial(fw, n, args, port, ledger, temporal, rng, k, scratch)
                rec["ledger"] = os.path.basename(ledger)
                rows.append(rec)
                out.write(json.dumps(rec) + "\n")
                out.flush()
                print(f"[{i}/{len(plan)}] {fw:15} key={'y' if k else 'n'} "
                      f"{rec['status']:20} dup={rec.get('duplicates', '-')} "
                      f"t={rec.get('killed_after', 0):.2f}", flush=True)
    finally:
        api.kill()
        if temporal_proc is not None:
            kill_tree(temporal_proc)
    summarise(rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
