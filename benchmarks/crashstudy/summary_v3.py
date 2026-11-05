"""The experiments that test what the paper applies its mechanisms to: signals, changed
prompts, long calls, a service manager, the kernel alone, and the simple baseline.

    python -m benchmarks.crashstudy.summary_v3

Nothing here calls a provider or runs a framework. Everything comes from the recorded rows.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from pathlib import Path

from .analyze import wilson
from .summary_v2 import SLEPT, dup, inflight, valid

V2 = Path(__file__).resolve().parent / "results" / "v2"
KEPT = ("ok", "failed")


def rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def kept(rs: list[dict]) -> list[dict]:
    return [r for r in rs if r.get("status") in KEPT]


def flagged(r: dict) -> bool:
    return bool((r.get("signals") or {}).get("announced") or (r.get("state") or {}).get("announced"))


def frac(n: int, d: int) -> str:
    return f"{n}/{d}"


def repeats(rs: list[dict]) -> str:
    return frac(sum(r.get("duplicates", 0) > 0 for r in rs), len(rs))


def signal_study() -> dict:
    """For each call charged twice, did a documented field change on its second execution?"""
    path = V2 / "signals" / "standin.jsonl"
    rs = kept(rows(path))
    out: dict = {}
    if not rs:
        return out
    db = sqlite3.connect(f"file:{(V2 / 'signals' / rs[0]['ledger']).as_posix()}?mode=ro", uri=True)
    for fw in sorted({r["framework"] for r in rs}):
        told = total = other = 0
        for r in (x for x in rs if x["framework"] == fw):
            charged = [c[0] for c in db.execute("SELECT i FROM charges WHERE run = ?", (r["run"],))]
            again = {i for i, n in Counter(charged).items() if n > 1}
            by: dict = {}
            for n in r["signals"].get("notes", []):
                by.setdefault(n["i"], []).append(n)

            def raised(n: dict) -> bool:
                return any((n.get(k) or 0) > 1 for k in n if k not in ("i", "pid", "first"))

            for i, execs in by.items():
                if i in again:
                    total += 1
                    told += any(raised(n) for n in execs[1:])
                else:
                    other += any(raised(n) for n in execs)
        out[fw] = {"crashes": sum(r["framework"] == fw for r in rs), "repeats_told": told,
                   "repeats": total, "other_calls_marked": other}
    db.close()
    return out


def changed_prompts() -> dict:
    d = V2 / "drift-frameworks"
    out: dict = {}
    for name, file in (("changed", "frameworks.jsonl"), ("changed", "in-unit.jsonl"), ("unchanged", "unchanged.jsonl")):
        for r in kept(rows(d / file)):
            s = out.setdefault(name, {}).setdefault(r["framework"], Counter())
            s["crashes"] += 1
            s["wrong"] += r.get("wrong_replies", 0) > 0
            s["repeat"] += r.get("duplicates", 0) > 0
            s["calls_repeated"] += r.get("duplicates", 0)
            s["failed"] += r["status"] == "failed"
    for name, file in (("agentos, no fingerprint", "agentos-nofp.jsonl"), ("agentos", "agentos.jsonl")):
        rs = kept(rows(d / file))
        out.setdefault("changed", {})[name] = Counter(
            crashes=len(rs), wrong=sum(r.get("wrong_replies", 0) > 0 for r in rs),
            repeat=sum(r.get("duplicates", 0) > 0 for r in rs),
            calls_repeated=sum(r.get("duplicates", 0) for r in rs),
            failed=sum(r["status"] == "failed" for r in rs))
    return out


def long_calls() -> dict:
    out: dict = {}
    for L in ("10", "30"):
        files = {f"windows {p.stem}": p for p in sorted((V2 / "long" / f"L{L}").glob("*.jsonl"))}
        files.update({f"linux {p.stem.split('-', 1)[1]}": p for p in sorted((V2 / "long" / "linux").glob(f"L{L}-*.jsonl"))})
        for name, p in files.items():
            rs = kept(rows(p))
            dup = [r for r in rs if r.get("duplicates", 0) > 0]
            out[f"L={L} {name}"] = {
                "repeated": repeats(rs), "flagged": frac(sum(flagged(r) for r in dup), len(dup)),
                "in_flight": sum(bool(r.get("kill_in_call")) for r in rs),
                "wrong": sum(r.get("wrong_replies", 0) > 0 for r in rs),
                "failed": sum(r["status"] == "failed" for r in rs),
                "fates": dict(Counter(f for r in rs for f in r.get("fates", []))),
            }
    return out


def real_long() -> dict:
    """Real calls of ten seconds or more on free tiers. Sends, an upper bound on the bill."""
    out: dict = {}
    for d in sorted((V2 / "long" / "real").glob("*")):
        for p in sorted(d.glob("*.jsonl")):
            rs = kept(rows(p))
            again = [r for r in rs if dup(r) > 0]
            out[f"{d.name} {p.stem}"] = {
                "repeated": frac(len(again), len(rs)), "flagged": frac(sum(flagged(r) for r in again), len(again)),
                "in_flight": sum(inflight(r) for r in rs), "wrong": sum(r.get("wrong_replies", 0) > 0 for r in rs),
                "median_call": sorted(r["latency"] for r in rs)[len(rs) // 2] if rs else None,
                "fates": dict(Counter(f for r in rs for f in r.get("fates", []))),
                "other": dict(Counter(r["status"] for r in rows(p) if r["status"] not in KEPT)),
            }
    return out


def plain_arms(sub: str) -> dict:
    out: dict = {}
    for p in sorted((V2 / sub).rglob("*.jsonl")):
        if "gateway" in p.name:
            continue
        rs = kept(rows(p))
        by: dict = {}
        for r in rs:
            by.setdefault(r.get("framework", "agentos"), []).append(r)
        for fw, v in by.items():
            dup = [r for r in v if r.get("duplicates", 0) > 0]
            out[f"{p.relative_to(V2 / sub).as_posix()} {fw}"] = {
                "repeated": repeats(v), "flagged": frac(sum(flagged(r) for r in dup), len(dup)),
                "in_flight": sum(bool(r.get("kill_in_call")) for r in v),
                "wrong": sum(r.get("wrong_replies", 0) > 0 or (r.get("state") or {}).get("wrong_replies", 0) > 0 for r in v),
                "failed": sum(r["status"] == "failed" for r in v),
                "missing": sum(r.get("missing", 0) > 0 for r in v),
                "fates": dict(Counter(f for r in v for f in r.get("fates", []))),
            }
    return out


def in_flight_rates() -> dict:
    """Mechanisms off, kills that land inside a call: a rate that travels to other workloads."""
    files = {"agent crash": [], "runtime crash": []}
    for root in ("windows/fix", "linux", "real/allam", "real/gpt-oss-120b", "real/qwen3.8-27b",
                 "multi/cohere-command-r7b", "multi/gemini-3.5-flash-lite"):
        files["agent crash"].append(V2 / root / "agent-adopt-no.jsonl")
        files["runtime crash"].append(V2 / root / "courier-off.jsonl")
    out = {}
    for crash, paths in files.items():
        rs = [r for p in paths for r in rows(p) if valid(r)]
        inside = [r for r in rs if inflight(r)]
        out[crash] = {"all": frac(sum(dup(r) > 0 for r in rs), len(rs)),
                      "in_flight": frac(sum(dup(r) > 0 for r in inside), len(inside))}
    return out


def exclusions() -> Counter:
    """Trials drawn again or left out, across every study but the seeded bugs and the sets set aside."""
    skip = ("mutants", "bazaarlink", "courier-before-flag-fix", "long/real/gemini")
    out: Counter = Counter()
    for p in V2.rglob("*.jsonl"):
        if any(s in p.as_posix() for s in skip) or "gateway-" in p.name or "calls.jsonl" in p.name:
            continue
        for r in rows(p):
            if "status" in r and r["status"] not in KEPT:
                out[r["status"]] += 1
            elif r.get("run") in SLEPT:  # recorded as failed, left out by hand: the machine slept
                out["slept"] += 1
    return out


def proxy_overhead() -> dict:
    """Per-call overhead from each arm's calibrated horizon: (H - L) / (N - 1) - T - L, in ms."""
    out: dict = {}
    for file in ("coalesce-L0.3.jsonl", "plain-L0.3.jsonl"):
        for r in kept(rows(V2 / "cache" / file)):
            out.setdefault(r["framework"], set()).add(round(1000 * ((r["horizon"] - r["latency"]) / (r["calls"] - 1) - r["think"] - r["latency"]), 1))
    plain: dict = {}
    for r in kept(rows(V2 / "signals" / "standin.jsonl")) + kept(rows(V2 / "windows" / "fix" / "courier-off.jsonl")):
        if r["framework"] in out:
            plain.setdefault(r["framework"], set()).add(round(1000 * ((r["horizon"] - r["latency"]) / (r["calls"] - 1) - r["think"] - r["latency"]), 1))
    return {fw: {"cache": sorted(v), "without": sorted(plain.get(fw, []))} for fw, v in out.items()}


def collect() -> dict:
    return {
        "exclusions": exclusions(), "proxy_overhead": proxy_overhead(),
        "signals": signal_study(), "changed_prompts": changed_prompts(), "long_calls": long_calls(),
        "real_long": real_long(),
        "kernel_kill": plain_arms("kernel-kill"), "service": plain_arms("service"),
        "cache": plain_arms("cache"), "in_flight": in_flight_rates(),
        "seeded_long": rows(V2 / "mutants" / "long-calls.jsonl"),
        "seeded_longer": rows(V2 / "mutants" / "longer-calls.jsonl"),
    }


def main() -> int:
    out = collect()
    print("== Do documented fields tell a re-executed call? (stand-in)")
    for fw, s in out["signals"].items():
        print(f"  {fw:16} repeats told {s['repeats_told']}/{s['repeats']}, other calls marked {s['other_calls_marked']}")
    print("== Every prompt changes after a restart (stand-in)")
    for name in ("changed", "unchanged"):
        for fw, s in out["changed_prompts"].get(name, {}).items():
            print(f"  {name:10} {fw:24} wrong reply {s['wrong']}/{s['crashes']}, repeat {s['repeat']}/{s['crashes']}, "
                  f"calls repeated {s['calls_repeated']}, failed {s['failed']}")
    for title, key in (("Long calls (stand-in, N=3)", "long_calls"), ("Real long calls, sends", "real_long"),
                       ("The kernel's process alone", "kernel_kill"),
                       ("A systemd service, main process killed", "service"),
                       ("A response cache outside the crash", "cache")):
        print(f"== {title}")
        for name, s in out[key].items():
            print(f"  {name:44} {s}")
    print("== Kills inside a call, mechanisms off")
    for crash, s in out["in_flight"].items():
        print(f"  {crash:14} all {s['all']}, in flight {s['in_flight']}")
    print("== Seeded bugs that need a courier still at work")
    for name, key in (("3 s calls", "seeded_long"), ("10 s calls", "seeded_longer")):
        for r in out[key]:
            print(f"  {name:10} {r['mutant']:34} n {r['n']} repeated {r['dup']} silent {r['silent']} wrong {r['wrong']} missing {r['missing']}")
    print("== Trials not counted, outside the seeded bugs:", dict(out["exclusions"]), sum(out["exclusions"].values()))
    print("== Overhead per call from calibrated horizons (ms):", out["proxy_overhead"])
    bound = lambda n: round(100 * wilson(0, n)[1], 1)  # noqa: E731
    print("== Upper 95% bounds on new zeros:", {n: bound(n) for n in (35, 39, 40, 80, 160, 320)})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
