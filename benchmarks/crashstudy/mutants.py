"""Which check finds which recovery bug? Seed one bug at a time and run them all.

    python3 -m benchmarks.crashstudy.mutants --trials 10 --out results/v2/mutants

Each mutant is a small edit to a throwaway copy of AgentOS: three reinstate bugs
found during this work, and the rest are bugs a recovery path could plausibly
have. The same battery of crashes runs against the clean copy and every mutant.
The bugs and the battery were written together, and two real bugs can fire only in
the changed-prompt and courier-alone arms, so the counts show that some bugs need a
given check, not how strong it is. POSIX only. The checks:

    duplicate   a crash repeated a call in an arm that should not repeat one
    missing     a run ended without making or answering all its calls
    reply       a reply failed the reply check (it answered another request)
    flag        a call was repeated with no flag, where a flag is the contract
    tests       the test modules that script crashes fail

Three kinds of crash are in the battery on purpose, and a mutant is reported as
found by a check only if that check fires where it stays quiet on the clean copy.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
KERNEL = "agentos/kernel/kernel.py"
COURIER = "agentos/runtime/courier.py"
STORE = "agentos/kernel/store.py"

# name, real bug?, file, old text, new text
MUTANTS = [
    ("old-reply-for-new-prompt", True, KERNEL,
     "def differs(",
     "def differs(*_a, **_k):\n    return False\n\n\ndef _differs("),
    ("courier-loss-not-flagged", True, KERNEL,
     '''        self.publish(
            "OutcomeAmbiguous", source_pid=proc.pid, pid=proc.pid,
            target=target, policy="courier",
        )
''', ""),
    ("spare-exit-not-retried", True, COURIER,
     '''            if self.row(key)["state"] != "dispatched":
                raise  # it ran, and failed
''', '''            raise
'''),
    ("reply-of-the-previous-call", False, COURIER,
     '''        row = self.latest(intent)
        if row is None:
            return "unsent", None
''', '''        row = self.latest(intent - 1) or self.latest(intent)
        if row is None:
            return "unsent", None
'''),
    ("intent-settled-too-early", False, STORE,
     '''    def open_intents(self) -> dict[int, dict[str, Any]]:
''', '''    def open_intents(self) -> dict[int, dict[str, Any]]:
        self.db.execute("UPDATE intents SET settled = 1")
'''),
    ("re-issue-not-flagged", False, KERNEL,
     '''                self.publish(
                    "OutcomeAmbiguous", source_pid=proc.pid, pid=proc.pid,
                    target=target, req_id=call.req_id, policy=self.on_ambiguous,
                )
''', ""),
    ("courier-stays-in-the-runtime", False, COURIER,
     'self.detach = os.environ.get("AGENTOS_COURIER_DETACH", "1") != "0"',
     'self.detach = False'),
    ("adopted-reply-dropped", False, KERNEL,
     '''        value = _shape({flight["dep"]: result})
        self.store.append_journal(
            proc.pid, req_id, flight["op"], value, None, intent=flight["intent"],
''', '''        return
        value = _shape({flight["dep"]: result})
        self.store.append_journal(
            proc.pid, req_id, flight["op"], value, None, intent=flight["intent"],
'''),
    ("running-courier-taken-as-unsent", False, COURIER,
     '''        if self.alive(row["key"]):
            return "running", row
''', '''        if self.alive(row["key"]):
            return "unsent", row
'''),
    ("courier-lock-not-held", False, COURIER,
     '''    def alive(self, key: str) -> bool:
        """Is a courier still holding this call?"""
''', '''    def alive(self, key: str) -> bool:
        """Is a courier still holding this call?"""
        return False
'''),
]

# The battery: arm name, flags for benchmarks.crashstudy.run, may it repeat a call?, is a flag owed?
ARMS = [
    ("adoption", ["--crash", "agent", "--adopt", "yes"], False, True),
    ("couriers", ["--courier", "on"], False, True),
    ("courier-killed-alone", ["--crash", "courier", "--courier", "on"], True, True),
    ("no-couriers", ["--courier", "off"], True, True),
    ("adoption-changed-prompt", ["--crash", "agent", "--adopt", "yes", "--drift", "tail"], True, False),
    ("couriers-changed-prompt", ["--courier", "on", "--drift", "tail"], True, False),
]
# Added after the first battery: calls long enough that recovery starts while a courier
# is still at work, which 0.3 s calls almost never reach. Reported apart from it.
LONG_ARM = ("couriers-long-calls", ["--courier", "on", "--latency", "3", "--think", "0.2"], False, True)
# Longer still, where recovery usually finds the courier at work.
LONGER_ARM = ("couriers-longer-calls", ["--courier", "on", "--latency", "10", "--think", "0.2",
                                        "--calls", "3"], False, True)
TESTS = ["tests.test_adoption", "tests.test_recovery", "tests.test_inflight", "tests.test_orphans"]


def copy_tree(dest: Path) -> None:
    ignore = shutil.ignore_patterns("__pycache__", "results", "*.pyc")
    for sub in ("agentos", "tests"):
        shutil.copytree(ROOT / sub, dest / sub, ignore=ignore)
    (dest / "benchmarks").mkdir()
    shutil.copy(ROOT / "benchmarks" / "__init__.py", dest / "benchmarks")
    shutil.copytree(ROOT / "benchmarks" / "crashstudy", dest / "benchmarks" / "crashstudy", ignore=ignore)


def apply(dest: Path, path: str, old: str, new: str) -> None:
    f = dest / path
    text = f.read_text(encoding="utf-8").replace("\r\n", "\n")
    if text.count(old) != 1:
        raise SystemExit(f"mutant does not apply cleanly to {path}: {old[:60]!r} x{text.count(old)}")
    f.write_text(text.replace(old, new), encoding="utf-8")


def battery(dest: Path, out: Path, args, arms=None) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "PYTHONPATH": str(dest), "PYTHONUTF8": "1"}
    res = {}
    for n, (arm, flags, may_repeat, flag_owed) in enumerate(arms or ARMS):
        path = (out / f"{arm}.jsonl").resolve()
        path.unlink(missing_ok=True)
        cmd = [args.python, "-m", "benchmarks.crashstudy.run", "--frameworks", "agentos", "--ignore-keys",
               "--keyed", "no", "--trials", str(args.trials), "--timeout", "60", "--calibrate", "1",
               "--seed", str(args.seed + n), "--out", str(path), *flags]
        try:
            subprocess.run(cmd, cwd=dest, env=env, capture_output=True, timeout=args.arm_timeout)
        except subprocess.TimeoutExpired:
            pass
        rows = []
        if path.exists():
            rows = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
        crashed = [r for r in rows if r.get("status") in ("ok", "failed")]
        dup = [r for r in crashed if r.get("duplicates", 0) > 0]
        res[arm] = {
            "n": len(crashed), "rows": len(rows), "dup": len(dup),
            "silent": sum(not (r.get("signals") or {}).get("announced") for r in dup),
            "missing": sum(r.get("missing", 0) > 0 or r.get("answered", 0) < r.get("calls", 0)
                           or r.get("status") == "failed" for r in crashed),
            "wrong": sum(r.get("wrong_replies", 0) > 0 for r in crashed),
            "may_repeat": may_repeat, "flag_owed": flag_owed,
        }
    return res


def unit_tests(dest: Path, args) -> dict:
    env = {**os.environ, "PYTHONPATH": str(dest), "PYTHONUTF8": "1"}
    try:
        r = subprocess.run([args.python, "-m", "unittest", *TESTS], cwd=dest, env=env,
                           capture_output=True, text=True, timeout=args.test_timeout)
        tail = r.stderr.strip().splitlines()[-1] if r.stderr.strip() else ""
        return {"failed": r.returncode != 0, "summary": tail}
    except subprocess.TimeoutExpired:
        return {"failed": True, "summary": "timed out"}


def checks(res: dict, clean: dict | None, tests: dict, clean_tests: dict | None) -> dict:
    """Which checks fire on this battery, beyond what the clean copy shows."""

    def more(arm: str, field: str) -> bool:
        return res[arm][field] > (clean[arm][field] if clean else 0)

    return {
        "duplicate": any(not a[2] and more(a[0], "dup") for a in ARMS),
        "missing": any(more(a[0], "missing") for a in ARMS),
        "reply": any(more(a[0], "wrong") for a in ARMS),
        "flag": any(a[3] and more(a[0], "silent") for a in ARMS),
        "tests": tests["failed"] and not (clean_tests or {}).get("failed", False),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--trials", type=int, default=10)
    ap.add_argument("--seed", type=int, default=701)
    ap.add_argument("--arm-timeout", type=float, default=240)
    ap.add_argument("--test-timeout", type=float, default=240)
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("--only", help="comma-separated mutant names, or 'clean'")
    ap.add_argument("--long-calls", help="comma-separated mutants, or clean: the long-call arm alone")
    ap.add_argument("--longer", action="store_true", help="with --long-calls: 10 s calls, not 3 s")
    ap.add_argument("--dry-run", action="store_true", help="check that every mutant applies, and stop")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    out = Path(args.out).resolve()
    only = set(args.only.split(",")) if args.only else None
    work = Path(tempfile.mkdtemp(prefix="crashstudy-mutants-"))
    if args.dry_run:
        for name, _real, path, old, new in MUTANTS:
            dest = work / name
            copy_tree(dest)
            apply(dest, path, old, new)
            subprocess.run([args.python, "-m", "py_compile", str(dest / path)], check=True)
            shutil.rmtree(dest)
            print("applies:", name, flush=True)
        return 0
    if args.long_calls:
        args.arm_timeout = max(args.arm_timeout, 2400 if args.longer else 900)
        long_arm = LONGER_ARM if args.longer else LONG_ARM
        for name in args.long_calls.split(","):
            dest = work / name
            copy_tree(dest)
            for mutant, _real, path, old, new in MUTANTS:
                if mutant == name:
                    apply(dest, path, old, new)
            arm = battery(dest, out / name, args, [long_arm])[long_arm[0]]
            with open(out / ("longer-calls.jsonl" if args.longer else "long-calls.jsonl"), "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"mutant": name, **arm}) + "\n")
            print(name, json.dumps(arm), flush=True)
            shutil.rmtree(dest, ignore_errors=True)
        return 0
    summary = out / "summary.jsonl"
    out.mkdir(parents=True, exist_ok=True)
    done = {}
    if summary.exists():
        for line in summary.read_text(encoding="utf-8").splitlines():
            r = json.loads(line)
            done[r["mutant"]] = r
    clean = done.get("clean")
    if clean is None and (only is None or "clean" in only):
        dest = work / "clean"
        copy_tree(dest)
        clean = {"mutant": "clean", "real": False, "arms": battery(dest, out / "clean", args),
                 "tests": unit_tests(dest, args)}
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(clean) + "\n")
        print("clean", json.dumps(clean), flush=True)
        shutil.rmtree(dest, ignore_errors=True)
    for name, real, path, old, new in MUTANTS:
        if name in done or (only and name not in only):
            continue
        dest = work / name
        copy_tree(dest)
        apply(dest, path, old, new)
        arms = battery(dest, out / name, args)
        tests = unit_tests(dest, args)
        rec = {"mutant": name, "real": real, "arms": arms, "tests": tests,
               "found_by": checks(arms, clean["arms"] if clean else None, tests,
                                  clean["tests"] if clean else None)}
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")
        print(name, json.dumps(rec["found_by"]), flush=True)
        shutil.rmtree(dest, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
