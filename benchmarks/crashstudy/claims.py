"""The numbers the paper states in prose rather than in a table, recomputed from the results.

    python -m benchmarks.crashstudy.claims

Each line shows the value from the recorded rows and the value in the paper. It exits
non-zero if any differ. Nothing here calls a provider.
"""

from __future__ import annotations

import json
import statistics
from collections import Counter
import sys
from math import comb
from pathlib import Path

from .analyze import load, overhead, predict, wilson
from .summary import valid
from . import summary_v3

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

    print("== RQ1: kills in flight, and silent repeats")
    v3 = summary_v3.collect()
    show("Groq survey, per-call kills with a call outstanding", frac(out), "286/377")
    for m, want in zip(MODELS, (75, 50, 39)):
        rs = [r for r in survey[m] if not r["keyed"] and r["framework"] not in WHOLE and r["framework"] != "agentos"]
        print(f"      {m}: six frameworks, per call, no key {frac(rs)}")
    six = [[r for r in survey[m] if not r["keyed"] and r["framework"] not in WHOLE and r["framework"] != "agentos"] for m in MODELS]
    rates = [round(100 * sum(r["duplicates"] > 0 for r in rs) / len(rs)) for rs in six]
    show("six frameworks, per call, range across models (%)", f"{min(rates)} to {max(rates)}", "39 to 75")
    sig = v3["signals"]
    show("Temporal's attempt, repeats told", f"{sig['temporal']['repeats_told']}/{sig['temporal']['repeats']}", "23/23")
    show("DBOS's recovery count, repeats told", f"{sig['dbos']['repeats_told']}/{sig['dbos']['repeats']}", "24/24")
    show("DBOS's recovery count, other calls marked", sig["dbos"]["other_calls_marked"], 79)
    quiet = sig["langgraph-node"]["repeats_told"] + sig["prefect"]["repeats_told"]
    show("LangGraph and Prefect, repeats told", f"{quiet}/{sig['langgraph-node']['repeats'] + sig['prefect']['repeats']}", "0/52")
    told = [r for r in every if not r["keyed"] and r["framework"] in ("temporal", "dbos") and r["duplicates"] > 0]
    others = [r for r in every if not r["keyed"] and r["framework"] != "agentos" and r["duplicates"] > 0]
    show("repeats on real models, no key, no signal", f"{len(others) - len(told)}/{len(others)}", "155/189")
    per = [r for r in others if r["framework"] not in WHOLE]
    per_told = [r for r in per if r["framework"] in ("temporal", "dbos")]
    show("per-call repeats, no key, silent", f"{len(per) - len(per_told)}/{len(per)}", "94/128")

    print("== RQ3: every prompt changed after a restart")
    ch = v3["changed_prompts"]["changed"]
    want = {"temporal-arg": 20, "dbos-arg": 17, "langgraph-func": 17, "prefect-arg": 0, "langgraph-node": 0, "crewai": 0,
            "crewai-checkpoint": 0, "autogen": 0, "agentos, no fingerprint": 20, "agentos": 0}
    for fw, n in want.items():
        show(f"{fw}, crashes with a wrong reply", f"{ch[fw]['wrong']}/{ch[fw]['crashes']}", f"{n}/20")
    for fw in ("temporal", "dbos"):
        show(f"{fw}, prompt built in the unit, wrong replies", f"{ch[fw]['wrong']}/{ch[fw]['crashes']}", "0/20")
    un = v3["changed_prompts"]["unchanged"]
    show("control, prompts unchanged, wrong replies", f"{sum(s['wrong'] for s in un.values())}/{sum(s['crashes'] for s in un.values())}", "0/80")
    show("Prefect, calls repeated per crash", ch["prefect-arg"]["calls_repeated"] / ch["prefect-arg"]["crashes"], 3.65)

    print("== RQ4: long calls, on both systems")
    lc = v3["long_calls"]
    def total(tag: str) -> str:
        v = [s["repeated"].split("/") for k, s in lc.items() if k.endswith(tag)]
        return f"{sum(int(a) for a, _ in v)}/{sum(int(b) for _, b in v)}"
    show("couriers", total("courier-on"), "0/160")
    show("adoption", total("agent-adopt-yes"), "0/160")
    show("no courier", total("courier-off"), "40/40")
    show("no adoption", total("agent-adopt-no"), "40/40")
    show("contained", total("courier-contained"), "79/80")
    fates = Counter()
    for k, s in lc.items():
        if k.endswith("courier-on"):
            fates.update(s["fates"])
    show("recovery found the courier running", f"{fates['running']}/{sum(fates.values())}", "149/159")

    rl = v3["real_long"]
    show("command-r7b, couriers", rl["cohere-command-r7b courier-on"]["repeated"], "0/15")
    show("command-r7b, no courier", rl["cohere-command-r7b courier-off"]["repeated"], "10/10")
    show("command-r7b, contained", rl["cohere-command-r7b courier-contained"]["repeated"], "6/6")
    show("command-r7b, median call (s)", round(rl["cohere-command-r7b courier-off"]["median_call"]), 13)
    f = rl["cohere-command-r7b courier-on"]["fates"]
    show("command-r7b, recovery found the courier running", f"{f['running']}/{sum(f.values())}", "13/14")

    print("== RQ4: a crash that reaches the courier, and the kernel alone")
    sv = v3["service"]
    show("systemd control-group, couriers", sv["control-group-courier.jsonl agentos"]["repeated"], "20/20")
    show("systemd mixed, couriers", sv["mixed-courier.jsonl agentos"]["repeated"], "20/20")
    show("systemd control-group, no courier", sv["control-group-no-courier.jsonl agentos"]["repeated"], "20/20")
    show("systemd process, couriers", sv["process-courier.jsonl agentos"]["repeated"], "0/20")
    kk = v3["kernel_kill"]
    show("kernel alone, couriers, Windows", kk["courier-on.jsonl agentos"]["repeated"], "0/30")
    show("kernel alone, couriers, Linux", kk["linux/courier-on.jsonl agentos"]["repeated"], "0/30")
    show("kernel alone, no courier, Windows", kk["courier-off.jsonl agentos"]["repeated"], "25/30")
    show("kernel alone, no courier, Linux", kk["linux/courier-off.jsonl agentos"]["repeated"], "20/30")

    print("== RQ4: a response cache outside the crash")
    def cached(prefix: str) -> str:
        v = [s["repeated"].split("/") for k, s in v3["cache"].items() if k.startswith(prefix)]
        return f"{sum(int(x) for x, _ in v)}/{sum(int(y) for _, y in v)}"
    show("merging, 0.3 and 3 s", f"{int(cached('coalesce').split('/')[0])}/{cached('coalesce').split('/')[1]}", "0/180")
    show("no merging, 0.3 s", cached("plain-L0.3"), "0/120")
    show("no merging, 3 s", cached("plain-L3"), "22/60")

    print("== RQ5: bugs that need a courier still at work, at 10 s calls")
    longer = {r["mutant"]: r for r in v3["seeded_longer"]}
    show("clean copy, repeats", longer["clean"]["dup"], 0)
    show("running courier taken as unsent, repeats", longer["running-courier-taken-as-unsent"]["dup"], 9)
    show("courier lock not held, repeats", longer["courier-lock-not-held"]["dup"], 8)

    print("== Threats: trials left out")
    show("of those, provider errors", v3["exclusions"]["provider-error"], 34)
    show("trials not counted, outside the seeded bugs", sum(v3["exclusions"].values()), 77)

    print("== Bounds on the zeros (95% Wilson, upper)")
    for n, want in ((380, 0.010), (320, 0.012), (190, 0.020), (263, 0.014), (140, 0.027), (40, 0.088), (15, 0.204)):
        show(f"0 of {n}", round(wilson(0, n)[1], 3), want)
    print(f"\n{'all claims match' if not bad else str(bad) + ' claims differ'}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
