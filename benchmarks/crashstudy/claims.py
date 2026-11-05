"""The numbers the paper states in prose rather than in a table, recomputed from the results.

    python -m benchmarks.crashstudy.claims

Each line shows the value from the recorded rows and the value in the paper. It exits
non-zero if any differ. Nothing here calls a provider.
"""

from __future__ import annotations

import json
import statistics
import sys
from math import comb
from pathlib import Path

from .analyze import load, overhead, predict, wilson
from .summary import valid

R = Path(__file__).resolve().parent / "results"
S = R / "standin"
WHOLE = {"langgraph-loop", "crewai"}
MODELS = ["gpt-oss-20b", "gpt-oss-120b", "qwen3.8-27b"]
bad = 0


def show(what: str, got, paper) -> None:
    global bad
    ok = got == paper
    bad += not ok
    print(f"  {'ok ' if ok else 'BAD'} {what:66} {got}" + ("" if ok else f"   (paper: {paper})"))


def rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def frac(rs: list[dict]) -> str:
    return f"{sum(r['duplicates'] > 0 for r in rs)}/{len(rs)}"


def fisher(a: int, n1: int, b: int, n2: int) -> float:
    k, N = a + b, n1 + n2
    p = lambda x: comb(n1, x) * comb(n2, k - x) / comb(N, k)  # noqa: E731
    return sum(p(x) for x in range(max(0, k - n2), min(k, n1) + 1) if p(x) <= p(a) * (1 + 1e-9))


def age(r: dict) -> float:
    return max(a for _, a in r["outstanding_at_kill"])


def main() -> int:
    survey = {m: [r for r in rows(R / "real" / f"real-main-{m}.jsonl") if valid(r)] for m in MODELS}
    every = [r for m in MODELS for r in survey[m]]
    per_call = [r for r in every if r["framework"] not in WHOLE]

    print("== RQ1: the survey on real models")
    show("per-call, no key", frac([r for r in per_call if not r["keyed"]]), "151/247")
    show("per-call, keyed", frac([r for r in per_call if r["keyed"]]), "139/242")
    for m, want in zip(MODELS, (78, 66, 39)):
        rs = [r for r in survey[m] if not r["keyed"] and r["framework"] not in WHOLE]
        show(f"per-call, no key, {m} (%)", round(100 * sum(r["duplicates"] > 0 for r in rs) / len(rs)), want)
    whole = [r for r in every if r["framework"] in WHOLE and not r["keyed"]]
    show("whole-run, no key", frac(whole), "61/63")
    show("whole-run, calls repeated per crash", round(statistics.mean(r["duplicates"] for r in whole), 2), 3.27)
    out = [r for r in per_call if r.get("outstanding_at_kill")]
    late = [r for r in out if age(r) >= 0.1]
    show("per-call kills 100 ms or more after the send", frac(late), "258/290")
    show("keyed against unkeyed, Fisher's exact p", round(fisher(139, 242, 151, 247), 2), 0.41)

    print("== RQ1: the window sweep")
    real = {t: [r for r in rows(R / "real" / f"real-sweep-T{t}.jsonl")
                if not r["keyed"] and r["status"] in ("ok", "failed")] for t in ("0", "0.1", "0.3", "0.9")}
    show("allam-2-7b, T=0", frac(real["0"]), "27/60")
    show("allam-2-7b, T=0.9", frac(real["0.9"]), "5/60")
    kills = [r for t in real for r in real[t] if r.get("outstanding_at_kill")]
    show("allam-2-7b kills within 50 ms of the send", frac([r for r in kills if age(r) < 0.05]), "0/49")
    show("allam-2-7b kills within 100 ms of the send", frac([r for r in kills if age(r) < 0.1]), "21/82")
    sw = load([str(S / "main.jsonl")] + [str(S / f"sweep-T{t}.jsonl") for t in ("0", "0.3", "0.9")])
    thinks: dict = {}
    for r in sw:
        thinks.setdefault(r["framework"], set()).add(r["think"])
    sw = [r for r in sw if len(thinks[r["framework"]]) > 1]
    extra = overhead(sw, str(S / "main-ledger.db"))
    show("measured overhead per call (ms)", round(1000 * extra), 26)
    groups: dict = {}
    for r in sw:
        if not r["keyed"] and r["status"] == "ok":
            groups.setdefault((r["framework"], r["think"]), []).append(int(r["duplicates"] > 0))
    inside = 0
    for (_, t), v in groups.items():
        lo, hi = wilson(sum(v), len(v))
        inside += lo <= predict("call", sw[0]["calls"], sw[0]["latency"], t, t + sw[0]["latency"] + extra)[0] <= hi
    show("stand-in intervals containing Eq. 1", f"{inside}/{len(groups)}", "12/12")

    print("== RQ1: whole-run units at N=12")
    n12 = rows(S / "n12.jsonl")
    for fw, want in (("langgraph-loop", 6.9), ("crewai", 5.45)):
        v = [r["duplicates"] for r in n12 if r["framework"] == fw and not r["keyed"] and r["status"] == "ok"]
        show(f"{fw}, calls repeated per crash", round(statistics.mean(v), 2), want)

    print("== RQ3: other frameworks, and the zeros")
    kill = R / "v2" / "windows" / "kill"
    show("Temporal split, workflow worker killed", frac([r for r in rows(kill / "temporal-workflow.jsonl") if r["status"] == "ok"]), "0/30")
    show("Temporal split, activity worker killed", frac([r for r in rows(kill / "temporal-activity.jsonl") if r["status"] == "ok"]), "22/30")
    gw = [R / "gateway" / "gateway-20261028-020008-gateway.jsonl", R / "gateway" / "gateway-20261029-031612-gateway.jsonl",
          kill / "gateway-20261103-010033-gateway.jsonl", *sorted((R / "gateway" / "real-gw-allam-on").glob("*/gateway-*.jsonl"))]
    logged = [r for f in gw for r in rows(f) if "decision" in r]
    show("gateway requests logged in the main arms", len(logged), 3512)
    show("of those, with a changed body", sum(bool(r.get("drift")) for r in logged), 0)
    ns = R / "v2" / "namespace"
    both = rows(ns / "gateway-no-courier.jsonl") + rows(ns / "gateway-courier.jsonl")
    both = [r for r in both if r["status"] in ("ok", "failed")]
    show("namespace killed, gateway outside", frac(both), "0/40")
    keys = [r for r in rows(ns / "keys-no-gateway.jsonl") if r["status"] in ("ok", "failed")]
    show("namespace killed, keys and no gateway", frac(keys), "19/20")
    show("  of those repeats, flagged", sum(bool(r["state"].get("announced")) for r in keys if r["duplicates"] > 0), 0)

    print("== Bounds on the zeros (95% Wilson, upper)")
    for n, want in ((380, 0.010), (190, 0.020), (263, 0.014), (140, 0.027), (40, 0.088), (15, 0.204)):
        show(f"0 of {n}", round(wilson(0, n)[1], 3), want)
    print(f"\n{'all claims match' if not bad else str(bad) + ' claims differ'}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
