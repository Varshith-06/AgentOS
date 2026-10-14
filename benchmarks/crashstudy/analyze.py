"""Tables (and, with matplotlib, figures) from crash-study results.

    python -m benchmarks.crashstudy.analyze results/main.jsonl [--latex out.tex]

Alongside each measurement it prints what the analytic model predicts for the
same parameters, so the two can be read side by side.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from pathlib import Path

# How much completed work each framework re-executes on resume, by construction:
# "call" re-runs only the call whose completion was not recorded; "run" re-runs
# everything the unit had done (LangGraph with the loop in one node, CrewAI
# without a guard: its restored flow starts again from @start).
GRANULARITY = {
    "agentos": "call",
    "langgraph-node": "call",
    "langgraph-loop": "run",
    "temporal": "call",
    "dbos": "call",
    "crewai": "run",
    "crewai-guarded": "call",
    "crewai-checkpoint": "call",
    "prefect": "call",
    "autogen": "call",
}

LABEL = {
    "agentos": "AgentOS (intent journal)",
    "langgraph-node": "LangGraph, node per call",
    "langgraph-loop": "LangGraph, loop in one node",
    "temporal": "Temporal, activity per call",
    "dbos": "DBOS, step per call",
    "crewai": "CrewAI Flow + @persist",
    "crewai-guarded": "CrewAI, guarded steps",
    "crewai-checkpoint": "CrewAI, CheckpointConfig",
    "prefect": "Prefect, cached tasks",
    "autogen": "AutoGen, save_state per call",
}


def predict(granularity: str, n: int, latency: float, think: float,
            period: float | None = None) -> tuple[float, float]:
    """(P(dup >= 1), E[dup]) for a kill uniform over the work after the first charge.

    The kill is drawn over the nominal horizon H = latency + (n-1)*(think+latency).
    Call j is charged at j*period and answered `latency` later; period defaults
    to think+latency, and a measured one adds the framework's own overhead.
    """
    nominal = think + latency
    period = period or nominal
    horizon = latency + (n - 1) * nominal
    starts = [j * period for j in range(n) if j * period < horizon]
    if granularity == "call":
        # A duplicate iff the kill lands between a charge and its reply.
        p = sum(min(s + latency, horizon) - s for s in starts) / horizon
        return p, p
    # "run": every call charged before the kill is repeated.
    return 1.0, sum(horizon - s for s in starts) / horizon


def periods(rows: list[dict], ledger: str) -> dict[str, float]:
    """Median time between consecutive calls per framework, from the ledger.
    The median, because a recovering Temporal worker waits ~10 s between two
    calls that are otherwise consecutive."""
    gaps: dict[str, list[float]] = {}
    for r in rows:
        if r["status"] != "ok":
            continue
        charges = _ledger(ledger, r).execute(
            "SELECT i, ts FROM charges WHERE run = ? ORDER BY id", (r["run"],)).fetchall()
        kill = charges[0][1] + r["killed_after"]
        for (i, a), (j, b) in zip(charges, charges[1:]):
            # Consecutive calls of one process: a pair spanning the kill
            # measures recovery, not the call cycle.
            if j == i + 1 and (b < kill or a > kill):
                gaps.setdefault(r["framework"], []).append(b - a)
    return {fw: statistics.median(g) for fw, g in gaps.items()}


_dbs: dict[str, object] = {}


def _ledger(ledger: str, r: dict):
    # Rows name their own ledger when the run wrote one beside the results.
    import sqlite3
    path = str(Path(ledger).with_name(r["ledger"])) if r.get("ledger") else ledger
    if path not in _dbs:
        _dbs[path] = sqlite3.connect(path)
    return _dbs[path]


def bootstrap_ci(xs: list[int], reps: int = 2000, seed: int = 0) -> tuple[float, float]:
    """95% percentile interval of the mean. Never leaves the data's range."""
    import random
    rng = random.Random(seed)
    means = sorted(statistics.mean(rng.choices(xs, k=len(xs))) for _ in range(reps))
    return means[int(0.025 * reps)], means[int(0.975 * reps) - 1]


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return float("nan"), float("nan")
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return max(0.0, c - h), min(1.0, c + h)


def load(paths: list[str]) -> list[dict]:
    rows = []
    for path in paths:
        with open(path, encoding="utf-8") as fh:
            rows += [json.loads(line) for line in fh if line.strip()]
    # A late kill (host stall) was drawn again by the run; it is not a sample.
    return [r for r in rows if r["status"] != "late"]


def table(rows: list[dict], per: dict[str, float] | None = None) -> list[dict]:
    """One row per (framework, keyed); with measured call periods (`per`), also
    the model evaluated at each framework's real call cycle."""
    per = per or {}
    groups: dict[tuple, list[dict]] = {}
    for r in rows:
        groups.setdefault((r["framework"], r["keyed"]), []).append(r)
    out = []
    for (fw, keyed), rs in groups.items():
        ok = [r for r in rs if r["status"] == "ok"]
        dups = [r["duplicates"] for r in ok]
        k = sum(d > 0 for d in dups)
        lo, hi = wilson(k, len(dups))
        p0 = rs[0]
        model = (GRANULARITY.get(fw, "call"), p0["calls"], p0["latency"], p0["think"])
        pp, pe = predict(*model)
        mp, me = predict(*model, per.get(fw))
        out.append({
            "framework": fw, "keyed": keyed, "trials": len(rs), "ok": len(ok),
            "p_dup": k / len(dups) if dups else float("nan"), "ci": (lo, hi),
            "e_dup": statistics.mean(dups) if dups else float("nan"),
            "max_dup": max(dups) if dups else 0,
            "pred_p": 0.0 if keyed else pp, "pred_e": 0.0 if keyed else pe,
            "meas_p": 0.0 if keyed else mp, "meas_e": 0.0 if keyed else me,
            "period": per.get(fw, float("nan")),
            "recovery": statistics.median([r["recovery_s"] for r in ok]) if ok else float("nan"),
            "in_call": sum(r["kill_in_call"] for r in ok),
            "dup_in_call": sum(r["kill_in_call"] and r["duplicates"] > 0 for r in ok),
            "dup_outside": sum((not r["kill_in_call"]) and r["duplicates"] > 0 for r in ok),
            "other": {s: sum(r["status"] == s for r in rs)
                      for s in {r["status"] for r in rs} if s != "ok"},
        })
    order = list(GRANULARITY)
    out.sort(key=lambda t: (t["keyed"], order.index(t["framework"])
                            if t["framework"] in order else 99))
    return out


def show(t: list[dict]) -> None:
    print(f"{'framework':16} {'key':3} {'ok/n':>7} {'P(dup)':>7} {'95% CI':>13} "
          f"{'pred':>5} {'meas':>5} {'E[dup]':>6} {'pred':>5} {'meas':>5} {'cycle':>5} "
          f"{'max':>3} {'in-call(dup)':>12} "
          f"{'dup-outside':>11} {'rec s':>6} other")
    for r in t:
        print(f"{r['framework']:16} {'y' if r['keyed'] else 'n':3} "
              f"{r['ok']:>3}/{r['trials']:<3} {r['p_dup']:>7.2f} "
              f"[{r['ci'][0]:.2f},{r['ci'][1]:.2f}] {r['pred_p']:>5.2f} {r['meas_p']:>5.2f} "
              f"{r['e_dup']:>6.2f} {r['pred_e']:>5.2f} {r['meas_e']:>5.2f} {r['period']:>5.3f} "
              f"{r['max_dup']:>3} "
              f"{r['in_call']:>5}({r['dup_in_call']:>2})      {r['dup_outside']:>5}    "
              f"{r['recovery']:>6.1f} {r['other'] or ''}")


def latex(t: list[dict], path: str) -> None:
    """The paper's results table: one row per configuration, without keys."""
    def num(x: float) -> str:  # .73, to fit one IEEE column
        return f"{x:.2f}".replace("0.", ".", 1) if x < 1 else f"{x:.2f}"
    lines = [
        r"\begin{tabular}{@{}lrcrrr@{}}",
        r"\toprule",
        r"& & & & \multicolumn{2}{c}{model $E$} \\ \cmidrule(l){5-6}",
        r"Configuration & $P$ & 95\% CI & $E$ & nom. & meas. \\",
        r"\midrule",
    ]
    for r in t:
        if r["keyed"]:
            continue
        label = LABEL.get(r["framework"], r["framework"]).replace("_", "\\_")
        lines.append(
            f"{label} & {num(r['p_dup'])} & [{num(r['ci'][0])}, {num(r['ci'][1])}] & "
            f"{num(r['e_dup'])} & {num(r['pred_e'])} & {num(r['meas_e'])} \\\\")
    lines += [r"\bottomrule", r"\end{tabular}"]
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")


def visibility(rows: list[dict]) -> None:
    """Could the application have known? Per framework, keyless crashes only:
    how many duplicates came with a signal (AgentOS's OutcomeAmbiguous, or an
    attempt number above one handed to the re-executed call), and how many
    signals came without a duplicate."""
    print(f"\n{'framework':18} {'crashes':>7} {'dups':>5} {'signalled':>9} {'false alarms':>12}  signal")
    by: dict[str, list[dict]] = {}
    for r in rows:
        if r["status"] == "ok" and not r["keyed"] and "signals" in r:
            by.setdefault(r["framework"], []).append(r)
    for fw, rs in sorted(by.items()):
        def flagged(r: dict) -> bool:
            s = r["signals"]
            return s.get("announced", 0) > 0 or any(a and a > 1 for _, a in s.get("attempts", []))
        kinds = {("OutcomeAmbiguous" if "announced" in r["signals"] else
                  "attempt > 1" if any(a is not None for _, a in r["signals"].get("attempts", []))
                  else "none exposed") for r in rs}
        dups = [r for r in rs if r["duplicates"]]
        print(f"{fw:18} {len(rs):>7} {len(dups):>5} {sum(map(flagged, dups)):>9} "
              f"{sum(flagged(r) for r in rs if not r['duplicates']):>12}  {', '.join(sorted(kinds))}")


def separation(rows: list[dict], ledger: str) -> None:
    """Per-call configurations: does time-since-last-charge decide duplication?

    Splits kills (seconds after the most recent charge) into those inside the
    call (< latency) and those after its reply, and prints how many of each
    duplicated, plus how long past the reply the late duplicates landed: the
    time the framework takes to record a reply it has already received.
    """
    # framework -> [[dup, n] inside the call, [dup, n] after the reply], late delays
    split: dict[str, list] = {}
    late: dict[str, list[float]] = {}
    for r in rows:
        if r["keyed"] or r["status"] != "ok" or GRANULARITY.get(r["framework"]) != "call":
            continue
        ts = [t for (t,) in _ledger(ledger, r).execute(
            "SELECT ts FROM charges WHERE run = ? ORDER BY id", (r["run"],))]
        kill = ts[0] + r["killed_after"]
        since = kill - max(t for t in ts if t <= kill)
        fw, inside = r["framework"], since < r["latency"]
        cell = split.setdefault(fw, [[0, 0], [0, 0]])[0 if inside else 1]
        cell[0] += bool(r["duplicates"])
        cell[1] += 1
        if r["duplicates"] and not inside:
            late.setdefault(fw, []).append(since - r["latency"])
    print(f"\n{'per-call config':16} {'dup|in call':>11} {'dup|after reply':>15}  "
          f"ms past the reply of each late duplicate")
    for fw in sorted(split):
        (a, n), (b, m) = split[fw]
        ms = ", ".join(f"{1000 * d:.0f}" for d in sorted(late.get(fw, [])))
        print(f"{fw:16} {a:>5}/{n:<5} {b:>9}/{m:<5}  {ms}")


INK, INK_2, GRID ="#0b0b0b", "#52514e", "#e4e3df"
SERIES = ["#2a78d6", "#eb6834", "#1baf7a"]  # first three validated slots
MARKERS = ["o", "s", "^"]


def _style(plt) -> None:
    plt.rcParams.update({
        "font.family": "serif", "font.size": 8, "axes.edgecolor": INK_2,
        "axes.labelcolor": INK, "xtick.color": INK_2, "ytick.color": INK_2,
        "axes.spines.top": False, "axes.spines.right": False,
        "axes.linewidth": 0.6, "pdf.fonttype": 42,
    })


def figure_bars(rows: list[dict], path: str, per: dict[str, float] | None = None) -> None:
    """E[dup] per configuration (no keys), 95% CI, with the model's prediction:
    at the nominal call cycle, and at the measured one when `per` is given."""
    import matplotlib.pyplot as plt
    _style(plt)
    t = [r for r in table(rows, per) if not r["keyed"]]
    groups: dict[str, list[int]] = {}
    for r in rows:
        if not r["keyed"] and r["status"] == "ok":
            groups.setdefault(r["framework"], []).append(r["duplicates"])
    t.sort(key=lambda r: r["e_dup"])
    labels = [LABEL.get(r["framework"], r["framework"]) for r in t]
    means = [r["e_dup"] for r in t]
    lo, hi = [], []
    for r in t:
        a, b = bootstrap_ci(groups[r["framework"]])
        lo.append(r["e_dup"] - a)
        hi.append(b - r["e_dup"])
    fig, ax = plt.subplots(figsize=(3.4, 2.5))
    y = range(len(t))
    ax.barh(y, means, height=0.62, color=SERIES[0], edgecolor="white",
            linewidth=1.0, xerr=[lo, hi], error_kw={"elinewidth": 0.7, "capsize": 1.5,
                                                "ecolor": INK})
    ax.scatter([r["pred_e"] for r in t], list(y), marker="D", s=14,
               facecolor="white", edgecolor=INK, linewidth=0.8, zorder=3,
               label="model (nominal)")
    if per:
        ax.scatter([r["meas_e"] for r in t], list(y), marker="D", s=14,
                   facecolor=INK, edgecolor="white", linewidth=0.6, zorder=4,
                   label="model (measured)")
    ax.set_yticks(list(y), labels)
    ax.set_xlabel("duplicate charges per crash")
    ax.xaxis.grid(True, color=GRID, linewidth=0.5)
    ax.set_axisbelow(True)
    # Room at the right of the short per-call bars for the legend.
    ax.set_xlim(0, 1.3 * max(r["pred_e"] for r in t))
    ax.legend(frameon=False, loc="lower right", fontsize=7, handletextpad=0.2,
              borderaxespad=0.1)
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)


def overhead(rows: list[dict], ledger: str) -> float:
    """Median time a framework adds to each call cycle beyond think + latency."""
    extra = []
    for r in rows:
        if r["status"] != "ok":
            continue
        charges = _ledger(ledger, r).execute(
            "SELECT i, ts FROM charges WHERE run = ? ORDER BY id", (r["run"],)).fetchall()
        kill = charges[0][1] + r["killed_after"]
        for (i, a), (j, b) in zip(charges, charges[1:]):
            if j == i + 1 and (b < kill or a > kill):
                extra.append(b - a - r["think"] - r["latency"])
    return statistics.median(extra)


def figure_sweep(rows: list[dict], path: str, extra: float | None = None) -> None:
    """P(dup) against the in-call time fraction, per framework, with the model:
    at the nominal cycle, and with the measured per-call overhead if given."""
    import matplotlib.pyplot as plt
    _style(plt)
    pts: dict[str, dict[float, list[int]]] = {}
    for r in rows:
        if r["keyed"] or r["status"] != "ok":
            continue
        frac = r["latency"] / (r["latency"] + r["think"])
        pts.setdefault(r["framework"], {}).setdefault(round(frac, 3), []).append(
            int(r["duplicates"] > 0))
    # Only frameworks swept over more than one ratio; at most three series.
    pts = dict(sorted((fw, by) for fw, by in pts.items() if len(by) > 1)[:3])
    fig, ax = plt.subplots(figsize=(3.4, 2.2))
    n, latency = rows[0]["calls"], rows[0]["latency"]
    xs = [i / 100 for i in range(20, 101)]
    thinks = [latency / x - latency for x in xs]
    ax.plot(xs, [predict("call", n, latency, t)[0] for t in thinks], color=INK_2,
            linewidth=1.0, linestyle="--", label="model (nominal)")
    if extra is not None:
        ax.plot(xs, [predict("call", n, latency, t, t + latency + extra)[0] for t in thinks],
                color=INK, linewidth=1.0,
                label=f"model (+{1000 * extra:.0f} ms/call)")
    for k, (fw, by) in enumerate(sorted(pts.items())):
        fr = sorted(by)
        ps = [statistics.mean(by[f]) for f in fr]
        ci = [wilson(sum(by[f]), len(by[f])) for f in fr]
        dodge = 0.015 * (k - 1)  # side by side, so the intervals don't overlap
        ax.errorbar([f + dodge for f in fr], ps, yerr=[[p - c[0] for p, c in zip(ps, ci)],
                                  [c[1] - p for p, c in zip(ps, ci)]],
                    fmt=MARKERS[k % 3], color=SERIES[k % 3], markersize=4,
                    elinewidth=0.7, capsize=1.5, label=LABEL.get(fw, fw).split(" ")[0].rstrip(","),
                    markeredgecolor="white", markeredgewidth=0.5)
    ax.set_xlabel("fraction of time inside calls, $L/(T+L)$")
    ax.set_ylabel("$P$(duplicate | crash)")
    ax.set_ylim(0, 1.05)
    ax.yaxis.grid(True, color=GRID, linewidth=0.5)
    ax.set_axisbelow(True)
    ax.legend(frameon=False, loc="upper left", fontsize=7, handlelength=1.6)
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("paths", nargs="+")
    ap.add_argument("--latex")
    ap.add_argument("--bars", help="write the E[dup] bar chart here (pdf/png)")
    ap.add_argument("--sweep", help="write the P(dup) sweep figure here (pdf/png)")
    ap.add_argument("--ledger", help="the run's ledger.db: check the in-flight window")
    args = ap.parse_args()
    rows = load(args.paths)
    if args.sweep:
        thinks: dict[str, set] = {}
        for r in rows:
            thinks.setdefault(r["framework"], set()).add(r["think"])
        rows = [r for r in rows if len(thinks[r["framework"]]) > 1]
        figure_sweep(rows, args.sweep, overhead(rows, args.ledger) if args.ledger else None)
        return 0
    per = periods(rows, args.ledger) if args.ledger else None
    t = table(rows, per)
    show(t)
    if args.ledger:
        separation(rows, args.ledger)
    visibility(rows)
    if args.latex:
        latex(t, args.latex)
    if args.bars:
        figure_bars(rows, args.bars, per)
    return 0


if __name__ == "__main__":
    sys.exit(main())
