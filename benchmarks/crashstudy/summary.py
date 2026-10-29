"""The paper's numbers from the real-model runs, the AgentOS fix, and the gateway.

    python -m benchmarks.crashstudy.summary [--latex paper/results-table.tex]

`analyze.py` reads stand-in runs and `realprovider.py summary` the Groq probes.
This joins them up: the main table (stand-in beside real models), what a kill
caught on real models, which repeats were flagged, the OpenRouter and Gemini
probes, the real-model sweep, and the fix table (adoption, couriers, gateway).
Everything is read from `results/`; nothing here touches a provider.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
from pathlib import Path

from benchmarks.crashstudy.analyze import LABEL, load as load_standin, periods, table, wilson

HERE = Path(__file__).resolve().parent
R = HERE / "results"
MODELS = ["gpt-oss-20b", "gpt-oss-120b", "qwen3.8-27b"]
WHOLE = {"langgraph-loop", "crewai"}  # recovery units that span the run


def rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def valid(r: dict) -> bool:
    """A crash that counts: killed on time, no provider error, and (on Groq) no
    idle gap longer than one quota refill, which would lose a request."""
    return (r["status"] in ("ok", "failed")
            and r.get("max_gap", 0) < r.get("refill", math.inf))


def frac(k: int, n: int) -> str:
    lo, hi = wilson(k, n)
    return f"{k}/{n} = {k / n:.2f} [{lo:.2f}, {hi:.2f}]"


def flagged(r: dict) -> bool:
    """Did the framework tell the re-executed call it may have run before?"""
    s = r.get("signals") or {}
    if r["framework"] == "temporal":
        return any((a or 0) > 1 for _, a in s.get("attempts", []))
    if r["framework"] == "agentos":
        return (s.get("announced") or 0) > 0
    return any(a not in (None, 0, 1) for _, a in s.get("attempts", []))


def main_table(latex: str | None) -> None:
    standin = load_standin([str(R / "standin" / "main.jsonl")])
    t = [x for x in table(standin, periods(standin, str(R / "standin" / "main-ledger.db")))
         if not x["keyed"]]
    real = [r for m in MODELS for r in rows(R / "real" / f"real-main-{m}.jsonl") if valid(r)]

    def num(x: float) -> str:  # .73, to fit one IEEE column
        return f"{x:.2f}".replace("0.", ".", 1) if x < 1 else f"{x:.2f}"

    lines = [r"\begin{tabular}{@{}lrcrr|rr@{}}", r"\toprule",
             r"& \multicolumn{4}{c|}{stand-in, no key (E1)} & \multicolumn{2}{c}{real (E2)} \\",
             r"\cmidrule(lr){2-5}\cmidrule(l){6-7}",
             r"Configuration & $P$ & 95\% CI & $E$ & model & no key & key \\", r"\midrule"]
    print("== Table III: stand-in (keyless) beside real models (pooled)")
    for x in t:
        fw = x["framework"]
        cells = []
        for keyed in (False, True):
            s = [r for r in real if r["framework"] == fw and r["keyed"] == keyed]
            cells.append(f"{sum(r['duplicates'] > 0 for r in s)}/{len(s)}")
        label = LABEL.get(fw, fw).replace("_", "\\_")
        lines.append(f"{label} & {num(x['p_dup'])} & [{num(x['ci'][0])}, {num(x['ci'][1])}] & "
                     f"{num(x['e_dup'])} & {num(x['meas_e'])} & {cells[0]} & {cells[1]} \\\\")
        print(f"  {fw:18} P {x['p_dup']:.2f} E {x['e_dup']:.2f} model {x['meas_e']:.2f} | "
              f"real {cells[0]:>6} keyed {cells[1]:>6}")
    lines += [r"\bottomrule", r"\end{tabular}"]
    if latex:
        Path(latex).write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"  wrote {latex}")


def real_main() -> None:
    every = {m: rows(R / "real" / f"real-main-{m}.jsonl") for m in MODELS}
    counted = [r for v in every.values() for r in v if r["status"] in ("ok", "failed")]
    real = [r for r in counted if valid(r)]
    print("\n== E2: the main run on real models")
    print(f"  valid crashes {len(real)}; excluded for a gap over one refill "
          f"{len(counted) - len(real)}; worst distance of a count from an integer "
          f"{max(abs(r['admitted'] - round(r['admitted'])) for r in real):.1e}")
    for m, v in every.items():
        print(f"  {m}: median call {statistics.median(r['latency'] for r in v):.2f} s")
    for unit, keep in (("per-call", lambda fw: fw not in WHOLE), ("whole-run", lambda fw: fw in WHOLE)):
        for keyed in (False, True):
            s = [r for r in real if keep(r["framework"]) and r["keyed"] == keyed]
            mean = statistics.mean(r["duplicates"] for r in s)
            print(f"  {unit:9} {'keyed' if keyed else 'no key'}: re-billed "
                  f"{frac(sum(r['duplicates'] > 0 for r in s), len(s))}, mean duplicates {mean:.2f}")

    pc = [r for r in real if r["framework"] not in WHOLE]
    out = [r for r in pc if r.get("outstanding_at_kill")]
    reb = [r for r in out if r["duplicates"] > 0]
    spared = [r for r in out if r["duplicates"] == 0]
    early = [r for r in spared if max(a for _, a in r["outstanding_at_kill"]) < 0.1]
    stray = [r for r in pc if r["duplicates"] > 0 and not r.get("outstanding_at_kill")]
    print(f"  per-call kills with a call outstanding {len(out)}, re-billed {len(reb)}; "
          f"not re-billed {len(spared)}, of which within 100 ms of the send {len(early)}; "
          f"re-billed with nothing outstanding {len(stray)}")

    print("  repeats flagged (keyless): ")
    for fw in ("temporal", "agentos", "dbos"):
        s = [r for r in real if r["framework"] == fw and not r["keyed"]]
        tp = sum(flagged(r) and r["duplicates"] > 0 for r in s)
        fp = sum(flagged(r) and r["duplicates"] == 0 for r in s)
        fn = sum(not flagged(r) and r["duplicates"] > 0 for r in s)
        print(f"    {fw:8} flagged {tp} duplicates, missed {fn}, false alarms {fp}"
              + (f", precision {tp / (tp + fp):.2f}" if tp + fp else ""))
    k = [r for r in real if r["framework"] == "agentos" and r["keyed"] and r["duplicates"] > 0]
    print(f"    agentos keyed: {len(k)} duplicates, flagged {sum(map(flagged, k))}")


def sweep() -> None:
    print("\n== E3: the sweep on a real model (keyless, pooled)")
    for t in ("0", "0.1", "0.3", "0.9"):
        s = [r for r in rows(R / "real" / f"real-sweep-T{t}.jsonl") if valid(r) and not r["keyed"]]
        if s:
            lat = statistics.median(r["latency"] for r in s)
            print(f"  T={t:>3}: median call {lat:.2f} s, in calls {lat / (lat + float(t)):.2f}, "
                  f"re-billed {frac(sum(r['duplicates'] > 0 for r in s), len(s))}")


def probes() -> None:
    print("\n== E5: OpenRouter and Gemini")
    cut = [r for r in rows(R / "real" / "real-or-inflight.jsonl") if r["killed"] and not r["saw_done"]]
    rec = [r for r in cut if r.get("record")]
    toks = [r["record"]["tokens_completion"] for r in rec]
    print(f"  OpenRouter, killed mid-request {len(cut)}: recorded as generations {len(rec)}, "
          f"cancelled {sum(r['record']['cancelled'] for r in rec)}, "
          f"completion tokens {min(toks)}-{max(toks)}")
    for name, path in (("OpenRouter", "real-or-idem.jsonl"), ("Gemini", "real-gemini-idem.jsonl")):
        p = [r for r in rows(R / "real" / path) if all(x["status"] == 200 for x in r["replies"])]
        print(f"  {name}, same-key pairs {len(p)}: separate ids {sum(not r['same_id'] for r in p)}, "
              f"idempotency headers seen {sum(bool(x['idem_headers']) for r in p for x in r['replies'])}")


def arm(path: Path, frameworks: set[str] | None = None) -> list[dict]:
    v = [r for r in rows(path) if valid(r)]
    return [r for r in v if frameworks is None or r["framework"] in frameworks]


def fix() -> None:
    print("\n== E6-E8: the fix (stand-in set to ignore keys; real = allam-2-7b)")
    F = R / "agentos-fix"
    sets = {"Windows": (F, "fix"), "Linux": (F / "linux", "fix"), "real": (F, "real-fix")}
    before, after = [0, 0], [0, 0]
    for where, (d, pre) in sets.items():
        for name, rest in (("agent crash, no adoption", "agent-adopt-no"),
                           ("agent crash, adoption", "agent-adopt-yes"),
                           ("runtime crash, no couriers", "courier-off"),
                           ("runtime crash, couriers", "courier-on"),
                           ("runtime crash, contained", "courier-contained")):
            v = arm(d / f"{pre}-{rest}.jsonl")
            if not v:
                continue
            k = sum(r["duplicates"] > 0 for r in v)
            keyless = [r for r in v if not r["keyed"] and r["duplicates"] > 0]
            print(f"  {where:7} {name:27} re-billed {frac(k, len(v))}; "
                  f"in flight {sum(bool(r.get('kill_in_call') or r.get('outstanding_at_kill')) for r in v)}; "
                  f"keyless duplicates flagged {sum(map(flagged, keyless))}/{len(keyless)}")
            if rest in ("agent-adopt-no", "courier-off"):
                before[0] += k
                before[1] += len(v)
            if rest in ("agent-adopt-yes", "courier-on"):
                after[0] += k
                after[1] += len(v)
    print(f"  agent and runtime crashes: without the fix {frac(*before)}, with it {frac(*after)}")

    # Couriers' cost: on the stand-in the calibrated horizon is L + (N-1)·cycle.
    for where, d in (("Windows", F), ("Linux", F / "linux")):
        h = {c: statistics.mean(r["horizon"] for r in arm(d / f"fix-courier-{c}.jsonl"))
             for c in ("off", "on")}
        print(f"  courier cost per call cycle, stand-in, {where}: "
              f"{1000 * (h['on'] - h['off']) / 5:.0f} ms")
    cyc = {c: sorted({round(r["cycle"], 3) for r in arm(F / f"real-fix-courier-{c}.jsonl")})
           for c in ("off", "on")}
    print(f"  calibrated call cycles on the real model (s): off {cyc['off']}, on {cyc['on']}")

    for name, path in (("stand-in, keys only", R / "gateway" / "gateway-none.jsonl"),
                       ("stand-in, keys + gateway", R / "gateway" / "gateway.jsonl"),
                       ("real, keys only", R / "gateway" / "real-gw-allam-off.jsonl"),
                       ("real, keys + gateway", R / "gateway" / "real-gw-allam-on.jsonl")):
        v = [r for r in arm(path) if r["keyed"] and r["framework"] != "agentos"]
        others = sorted({r["framework"] for r in v})
        print(f"  other frameworks, {name:24} re-billed {frac(sum(r['duplicates'] > 0 for r in v), len(v))}; "
              f"in flight {sum(bool(r.get('kill_in_call') or r.get('outstanding_at_kill')) for r in v)}; "
              f"{len(others)} configurations")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--latex", help="write the main results table here")
    args = ap.parse_args()
    main_table(args.latex)
    real_main()
    sweep()
    probes()
    fix()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
