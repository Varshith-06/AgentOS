"""The paper's numbers from results/v2: every experiment `run_v2` runs.

    python -m benchmarks.crashstudy.summary_v2 [--json out.json]

Reads files only. A cell is a count of valid crashes; what was left out, and why,
is printed beside it, so that no denominator hides anything.
"""

from __future__ import annotations

import argparse
import collections
import json
import math
import statistics
from pathlib import Path

V2 = Path(__file__).resolve().parent / "results" / "v2"
KEPT = {"ok", "failed"}
# Trials the machine slept through (lid closed, 2 Nov 23:22:29-23:34:17 IST, Windows
# events 506 and 507). The raw rows stay in the result files; they are not counted.
SLEPT = {"temporal-u-11-cba417ab": "resume timed out across the sleep"}
OUT: dict = {}


def rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def valid(r: dict) -> bool:
    return r.get("run") not in SLEPT and r.get("status") in KEPT and r.get("max_gap", 0) < r.get("refill", math.inf)


def wilson_hi(k: int, n: int, z: float = 1.96) -> float:
    if not n:
        return float("nan")
    k = min(k, n)  # a repeat can come from outside this denominator
    p, d = k / n, 1 + z * z / n
    return (p + z * z / (2 * n) + z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))) / d


def fisher(a: int, b: int, c: int, d: int) -> float:
    n, r, k = a + b + c + d, a + b, a + c
    pr = lambda x: math.comb(r, x) * math.comb(n - r, k - x) / math.comb(n, k)  # noqa: E731
    p0 = pr(a)
    return sum(pr(x) for x in range(max(0, k - (n - r)), min(r, k) + 1) if pr(x) <= p0 * (1 + 1e-9))


def dup(r: dict) -> int:
    """Calls billed or sent beyond the workload's."""
    return r["resent"] if "resent" in r else r.get("duplicates", 0)


def inflight(r: dict) -> bool:
    return bool(r["outstanding_at_kill"]) if "outstanding_at_kill" in r else bool(r.get("kill_in_call"))


def flagged(r: dict) -> bool:
    return ((r.get("signals") or {}).get("announced") or 0) > 0


def diverged(r: dict) -> bool:
    return ((r.get("signals") or {}).get("diverged") or 0) > 0


def cell(path: Path, label: str = "") -> dict:
    rs = rows(path)
    v = [r for r in rs if valid(r)]
    d = [r for r in v if dup(r) > 0]
    left = collections.Counter("slept" if r.get("run") in SLEPT else
                               r["status"] if r["status"] not in KEPT else "gap"
                               for r in rs if not valid(r))
    c = {"n": len(v), "dup": len(d), "extra": sum(max(0, dup(r)) for r in v),
         "inflight": sum(inflight(r) for r in v),
         "flagged": sum(flagged(r) for r in d), "diverged": sum(diverged(r) for r in v),
         "wrong": sum((r.get("wrong_replies") or 0) > 0 for r in v),
         "failed": sum(r["status"] == "failed" for r in v),
         "unanswered": sum(r.get("answered", r.get("calls")) is not None and
                           r.get("answered", 0) < r.get("calls", 0) * (r.get("agents") or 1) for r in v),
         "missing": sum((r.get("missing") or 0) > 0 for r in v),
         "left_out": dict(left), "rows": len(rs),
         "recovery": statistics.median([r["recovery_s"] for r in v if r.get("recovery_s")])
         if any(r.get("recovery_s") for r in v) else None,
         "latency": statistics.median([r["latency"] for r in v]) if v else None,
         "horizon": statistics.median([r["horizon"] for r in v]) if v else None}
    c["hi_all"], c["hi_inflight"] = wilson_hi(c["dup"], c["n"]), wilson_hi(c["dup"], c["inflight"])
    if label:
        print(f"  {label:34} {c['dup']:>3}/{c['n']:<3} inflight {c['inflight']:>3} flagged {c['flagged']:>3} "
              f"diverged {c['diverged']:>3} wrong {c['wrong']:>3} failed {c['failed']:>2} unans {c['unanswered']:>2} "
              f"extra {c['extra']:>3} rec {c['recovery'] or 0:5.1f}s L {c['latency'] or 0:4.2f} "
              f"hi {c['hi_all']:.3f}/{c['hi_inflight']:.3f} out {c['left_out'] or ''}")
    return c


FIX = ["agent-adopt-no", "agent-adopt-yes", "courier-off", "courier-on", "courier-contained"]


def section(title: str, d: Path, names: list[str] | None = None) -> dict:
    print(f"\n== {title}")
    names = names or sorted(p.stem for p in d.glob("*.jsonl")
                            if not p.stem.endswith("-gateway") and "-before-" not in p.stem)
    res = {n: cell(d / f"{n}.jsonl", n) for n in names if (d / f"{n}.jsonl").exists()}
    OUT[title] = res
    return res


def by_framework(title: str, path: Path) -> None:
    print(f"== {title}")
    groups: dict = collections.defaultdict(list)
    for r in rows(path):
        groups[r["framework"]].append(r)
    res = {}
    for fw, rs in sorted(groups.items()):
        tmp = path.with_name(f".{path.stem}-{fw}.tmp")
        tmp.write_text("\n".join(json.dumps(r) for r in rs), encoding="utf-8")
        res[fw] = cell(tmp, fw)
        tmp.unlink()
    OUT[title] = res


def real_task() -> None:
    """The code review, with the agent building each prompt from its own notes."""
    section("Real task, stand-in, exact counts", V2 / "task" / "standin")
    section("Real task, Gemini, sends", V2 / "task" / "gemini-3.5-flash-lite")


def model_route() -> None:
    """The kernel's own model routing, in place of a tool driver."""
    section("Model route, stand-in, Windows", V2 / "model" / "windows")
    section("Model route, stand-in, Linux", V2 / "model" / "linux")
    section("Model route, allam, exact counts", V2 / "model" / "allam")


def model_fit() -> None:
    print("\n== E-latency: rate against the model NL/H, stand-in, by framework")
    res = {}
    for f in sorted((V2 / "windows" / "latency").glob("L*.jsonl")):
        if f.stem.split("-")[-1] in ("adopt", "courier", "contained"):
            continue
        groups: dict = collections.defaultdict(list)
        for r in rows(f):
            if valid(r):
                groups[r["framework"]].append(r)
        for fw, rs in sorted(groups.items()):
            pred = statistics.mean(min(1, r["calls"] * r["latency"] / r["horizon"]) for r in rs)
            k = sum(dup(r) > 0 for r in rs)
            print(f"  {f.stem:6} {fw:16} {k:>2}/{len(rs):<2} = {k / len(rs):.2f}  model {pred:.2f}")
            res[f"{f.stem}/{fw}"] = {"k": k, "n": len(rs), "model": pred}
    OUT["latency-fit"] = res


def costs(path: Path, title: str) -> None:
    rs = [r for r in rows(path) if r["calls"] == r["expected"] and r["rc"] == 0]
    if not rs:
        return
    print(f"\n== {title}: median over runs (per-call overhead beyond think + latency)")
    groups: dict = collections.defaultdict(list)
    for r in rs:
        groups[r["arm"]].append(r)
    res = {}
    for arm, g in groups.items():
        med = lambda k: statistics.median(x[k] for x in g if x.get(k) is not None)  # noqa: E731
        gaps = sorted(v for x in g for v in x["gaps"])
        res[arm] = {"n": len(g), "overhead_ms": med("overhead_ms"), "startup": med("startup"),
                    "rss": med("peak_rss_mb"), "cpu": med("cpu_s"),
                    "p95_cycle_ms": 1000 * gaps[int(.95 * len(gaps)) - 1] if gaps else None}
        print(f"  {arm:28} n={len(g):>2} over {res[arm]['overhead_ms']:7.1f} ms "
              f"(p95 cycle {res[arm]['p95_cycle_ms'] or 0:6.0f} ms) start {res[arm]['startup']:5.2f} s "
              f"rss {res[arm]['rss']:6.0f} MB cpu {res[arm]['cpu']:6.2f} s")
    OUT[title] = res


def host() -> None:
    print("\n== E-host: the VM stops (stopped VM, not pulled power)")
    res = {}
    for f in sorted((V2 / "host").glob("*.jsonl")):
        rs = rows(f)
        v = [r for r in rs if r["status"] in KEPT]
        d = [r for r in v if r.get("duplicates", 0) > 0]
        silent = [r for r in d if not (r.get("state") or {}).get("announced")]
        lost = [r for r in v if r.get("missing", 0) > 0]
        nostore = [r for r in v if not (r.get("state") or {}).get("store")]
        res[f.stem] = {"n": len(v), "dup": len(d), "silent": len(silent), "lost_calls": len(lost),
                       "no_store": len(nostore), "rows": len(rs), "extra": sum(r.get("duplicates", 0) for r in v)}
        print(f"  {f.stem:18} {res[f.stem]}")
    OUT["host"] = res


def probes() -> None:
    print("\n== Provider probes: one Idempotency-Key, two requests")
    groups: dict = collections.defaultdict(list)
    for r in rows(V2 / "probes.jsonl"):
        groups[(r["provider"], r["model"])].append(r)
    res = {}
    for (p, m), rs in groups.items():
        ok = [r for r in rs if all(x["status"] == 200 for x in r["replies"])]
        res[f"{p}/{m}"] = {"pairs": len(ok), "attempted": len(rs),
                           "same_id": sum(r["same_id"] for r in ok),
                           "same_text": sum(r["same_text"] for r in ok), "router": rs[0]["router"],
                           "prompt_tokens": sum((x.get("usage") or {}).get("prompt_tokens") or 0 for r in ok for x in r["replies"])}
        print(f"  {p + '/' + m:50} {res[f'{p}/{m}']}")
    OUT["probes"] = res


def tokens() -> None:
    """Tokens per call in the long-reply runs, for a price to be applied to."""
    print("\n== Tokens per call, from the providers' own usage figures")
    res = {}
    for d in sorted((V2 / "multi").glob("*")):
        if not d.is_dir():
            continue
        v = [r for f in d.glob("*.jsonl") for r in rows(f) if valid(r) and r.get("sends")]
        if v:
            pt, ct = sum(r["prompt_tokens"] for r in v), sum(r["completion_tokens"] for r in v)
            n = sum(r["billable_sends"] for r in v)
            res[d.name] = {"prompt": pt / n, "completion": ct / n, "sends": n}
            print(f"  {d.name:32} prompt {pt / n:6.1f} completion {ct / n:6.1f} over {n} sends")
    OUT["tokens"] = res


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json")
    args = ap.parse_args()
    w = V2 / "windows"
    section("E6 Windows, stand-in", w / "fix", FIX)
    section("E7 Linux, stand-in", V2 / "linux", FIX)
    for m in ("allam", "gpt-oss-120b", "qwen3.8-27b"):
        section(f"Real, exact counts: {m}", V2 / "real" / m)
    if (V2 / "multi").exists():
        for d in sorted((V2 / "multi").glob("*")):
            if d.is_dir():
                section(f"Elsewhere, sends: {d.name}", d)
    section("Changed prompts, stand-in", w / "drift")
    section("Linux, others", V2 / "linux", ["drift-agent-tail", "drift-courier-tail", "double-courier-on"])
    section("Longer calls, fixes", w / "latency",
            [p.stem for p in sorted((w / "latency").glob("L*-*.jsonl"))])
    real_task()
    model_route()
    model_fit()
    section("Recovery killed too", w / "double", ["courier-off", "courier-on", "adopt"])
    by_framework("Recovery killed too, others", w / "double" / "others.jsonl")
    section("One process killed", w / "kill", ["courier", "temporal-workflow", "temporal-activity"])
    by_framework("Gateway killed with the run", w / "kill" / "gateway.jsonl")
    section("Agents side by side", w / "parallel")
    section("N=12", w / "n12")
    host()
    probes()
    tokens()
    costs(w / "costs.jsonl", "Costs, Windows")
    costs(V2 / "linux" / "costs.jsonl", "Costs, Linux")
    if args.json:
        Path(args.json).write_text(json.dumps(OUT, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
