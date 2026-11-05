"""Every experiment of the November re-run, in order, resumable.

    python -m benchmarks.crashstudy.run_v2 --python <py3.13 with the frameworks>

Runs one stage at a time. Inside a stage, groups run side by side and each group
runs its commands in order: side by side only where the work is waiting on a
remote model, since the stand-in's timing would suffer from company. A command
that has finished leaves a marker and is not run again. Keys come from the
environment file named by --keys; nothing here prints them.
"""

from __future__ import annotations

import argparse
import ctypes
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
V2 = "benchmarks/crashstudy/results/v2"
RUN = "-m benchmarks.crashstudy.run --timeout 300"
REAL = "-m benchmarks.crashstudy.realmain"
MULTI = "-m benchmarks.crashstudy.multi"
PROBE = "-m benchmarks.crashstudy.otherproviders compat-idem"


def fix_arms(out: str, seed: int, trials: int = 30, extra: str = "--keyed both") -> list[tuple]:
    """Adoption off and on, couriers off, on and contained: E6 and E7."""
    base = f"{RUN} --frameworks agentos --ignore-keys --trials {trials} --calibrate 5 {extra}"
    return [(f"{base} --crash agent --adopt no --seed {seed} --out {out}/agent-adopt-no.jsonl", {}),
            (f"{base} --crash agent --adopt yes --seed {seed + 1} --out {out}/agent-adopt-yes.jsonl", {}),
            (f"{base} --courier off --seed {seed + 2} --out {out}/courier-off.jsonl", {}),
            (f"{base} --courier on --seed {seed + 3} --out {out}/courier-on.jsonl", {}),
            (f"{base} --courier contained --seed {seed + 4} --out {out}/courier-contained.jsonl", {})]


def stand_in() -> list[tuple]:
    w = f"{V2}/windows"
    cmds = fix_arms(f"{w}/fix", 201)
    # The prompt changes across a crash: the fix, and the control without it.
    a = f"{RUN} --frameworks agentos --ignore-keys --keyed no --trials 30 --calibrate 5"
    for name, flags in [("agent-tail", "--crash agent --drift tail"),
                        ("agent-tail-nofp", "--crash agent --drift tail --no-fingerprint"),
                        ("agent-all", "--crash agent --drift all"),
                        ("courier-tail", "--courier on --drift tail"),
                        ("courier-tail-nofp", "--courier on --drift tail --no-fingerprint"),
                        ("courier-all", "--courier on --drift all")]:
        cmds.append((f"{a} {flags} --seed 211 --out {w}/drift/{name}.jsonl", {}))
    g = (f"{RUN} --frameworks langgraph-node,dbos,prefect --ignore-keys --keyed yes --gateway "
         "--trials 20 --calibrate 3 --drift all")
    cmds += [(f"{g} --on-drift replay --seed 221 --out {w}/drift/gateway-replay.jsonl", {}),
             (f"{g} --on-drift reject --seed 222 --out {w}/drift/gateway-reject.jsonl", {}),
             (f"{g} --on-drift reissue --seed 223 --out {w}/drift/gateway-reissue.jsonl", {})]
    # Longer calls: is the rate what the model says, and do the fixes still hold?
    for L in ("0.1", "1", "3"):
        cmds.append((f"{RUN} --frameworks agentos,langgraph-node,dbos,temporal --ignore-keys --keyed no "
                     f"--trials 20 --calibrate 3 --latency {L} --seed 23{L[0]} "
                     f"--out {w}/latency/L{L}.jsonl", {"CS_ACTIVITY_TIMEOUT": "30" if L != "0.1" else "3"}))
    for L in ("1", "3"):
        f = f"{RUN} --frameworks agentos --ignore-keys --keyed no --trials 20 --calibrate 3 --latency {L}"
        cmds += [(f"{f} --crash agent --adopt yes --seed 24{L} --out {w}/latency/L{L}-adopt.jsonl", {}),
                 (f"{f} --courier on --seed 25{L} --out {w}/latency/L{L}-courier.jsonl", {}),
                 (f"{f} --courier contained --seed 26{L} --out {w}/latency/L{L}-contained.jsonl", {})]
    # Recovery is killed as well.
    d = f"{RUN} --ignore-keys --keyed no --trials 30 --calibrate 3"
    cmds += [(f"{d} --frameworks agentos --courier on --double 3 --seed 271 --out {w}/double/courier-on.jsonl", {}),
             (f"{d} --frameworks agentos --courier off --double 3 --seed 272 --out {w}/double/courier-off.jsonl", {}),
             (f"{d} --frameworks agentos --crash agent --adopt yes --double 1.5 --seed 273 --out {w}/double/adopt.jsonl", {}),
             (f"{d} --frameworks langgraph-node,dbos --double 3 --seed 274 --out {w}/double/others.jsonl", {})]
    # The process holding the call dies alone.
    cmds += [(f"{d} --frameworks agentos --crash courier --courier on --seed 281 --out {w}/kill/courier.jsonl", {}),
             (f"{d} --frameworks temporal-split --crash agent --seed 282 --out {w}/kill/temporal-workflow.jsonl", {}),
             (f"{d} --frameworks temporal-split --crash activity --seed 283 --out {w}/kill/temporal-activity.jsonl", {}),
             (f"{RUN} --frameworks langgraph-node,dbos,autogen --ignore-keys --keyed yes --gateway --kill-gateway "
              f"--trials 30 --calibrate 3 --seed 284 --out {w}/kill/gateway.jsonl", {})]
    # Agents side by side, and longer runs.
    for n in (4, 8):
        for c in ("off", "on"):
            cmds.append((f"{RUN} --frameworks agentos --ignore-keys --keyed no --trials 20 --calibrate 3 "
                         f"--agents {n} --courier {c} --seed 29{n} --out {w}/parallel/x{n}-{c}.jsonl", {}))
    c12 = f"{RUN} --frameworks agentos --ignore-keys --keyed no --trials 20 --calibrate 3 --calls 12"
    cmds += [(f"{c12} --crash agent --adopt no --seed 301 --out {w}/n12/adopt-no.jsonl", {}),
             (f"{c12} --crash agent --adopt yes --seed 302 --out {w}/n12/adopt-yes.jsonl", {}),
             (f"{c12} --courier off --seed 303 --out {w}/n12/courier-off.jsonl", {}),
             (f"{c12} --courier on --seed 304 --out {w}/n12/courier-on.jsonl", {})]
    cmds.append((f"-m benchmarks.crashstudy.costs --runs 10 --out {w}/costs.jsonl", {}))
    return cmds


def groq_allam() -> list[tuple]:
    r, m = f"{V2}/real/allam", "--model allam-2-7b --frameworks agentos --calibrate 5"
    cmds = [(f"{REAL} {m} --crash agent --adopt no --trials 10 --out {r}/agent-adopt-no.jsonl", {}),
            (f"{REAL} {m} --crash agent --adopt yes --trials 10 --out {r}/agent-adopt-yes.jsonl", {}),
            (f"{REAL} {m} --courier off --trials 10 --out {r}/courier-off.jsonl", {}),
            (f"{REAL} {m} --courier on --trials 10 --out {r}/courier-on.jsonl", {}),
            (f"{REAL} {m} --courier contained --trials 10 --out {r}/courier-contained.jsonl", {})]
    k = f"{m} --keyed no --trials 20"
    cmds += [(f"{REAL} {k} --crash agent --out {r}/drift-agent-tail.jsonl", {"CS_DRIFT": "tail"}),
             (f"{REAL} {k} --crash agent --out {r}/drift-agent-tail-nofp.jsonl", {"CS_DRIFT": "tail", "CS_NOFP": "1"}),
             (f"{REAL} {k} --courier on --out {r}/drift-courier-tail.jsonl", {"CS_DRIFT": "tail"}),
             (f"{REAL} {k} --courier on --chain --out {r}/chain-courier.jsonl", {}),
             (f"{REAL} {k} --crash agent --chain --out {r}/chain-agent.jsonl", {})]
    long = {"CS_WORDS": "150", "CS_MAX_TOKENS": "400"}
    l = "--model allam-2-7b --keyed no --trials 10 --calibrate 3"
    cmds += [(f"{REAL} {l} --frameworks agentos --crash agent --adopt no --out {r}/long-adopt-no.jsonl", long),
             (f"{REAL} {l} --frameworks agentos --crash agent --adopt yes --out {r}/long-adopt-yes.jsonl", long),
             (f"{REAL} {l} --frameworks agentos --courier off --out {r}/long-courier-off.jsonl", long),
             (f"{REAL} {l} --frameworks agentos --courier on --out {r}/long-courier-on.jsonl", long),
             (f"{REAL} {l} --frameworks langgraph-node,dbos --out {r}/long-others.jsonl", long)]
    return cmds


def groq_other(model: str, slug: str) -> list[tuple]:
    r, m = f"{V2}/real/{slug}", f"--model {model} --frameworks agentos --keyed no --trials 15 --calibrate 3"
    return [(f"{REAL} {m} --crash agent --adopt no --out {r}/agent-adopt-no.jsonl", {}),
            (f"{REAL} {m} --crash agent --adopt yes --out {r}/agent-adopt-yes.jsonl", {}),
            (f"{REAL} {m} --courier off --out {r}/courier-off.jsonl", {}),
            (f"{REAL} {m} --courier on --out {r}/courier-on.jsonl", {})]


def elsewhere(via: str, model: str, slug: str, extra: str = "", trials: int = 10,
              probe_only: bool = False) -> list[tuple]:
    """A slower provider: sends rather than admissions, the fix and its baselines."""
    o, m = f"{V2}/multi/{slug}", f"--via {via} --model {model} --trials {trials} {extra}"
    cmds = [(f"{PROBE} --via {via} --model {model} --trials 10 --out {V2}/probes.jsonl", {})]
    if probe_only:
        return cmds
    cmds += [(f"{MULTI} {m} --framework agentos --crash agent --adopt no --seed 401 --out {o}/agent-adopt-no.jsonl", {}),
             (f"{MULTI} {m} --framework agentos --crash agent --adopt yes --seed 402 --out {o}/agent-adopt-yes.jsonl", {}),
             (f"{MULTI} {m} --framework agentos --courier off --seed 403 --out {o}/courier-off.jsonl", {}),
             (f"{MULTI} {m} --framework agentos --courier on --seed 404 --out {o}/courier-on.jsonl", {}),
             (f"{MULTI} {m} --framework agentos --courier contained --seed 405 --out {o}/courier-contained.jsonl", {}),
             (f"{MULTI} {m} --framework agentos --crash agent --drift tail --seed 406 --out {o}/drift-agent-tail.jsonl", {}),
             (f"{MULTI} {m} --framework langgraph-node --seed 407 --out {o}/langgraph-node.jsonl", {}),
             (f"{MULTI} {m} --framework dbos --seed 408 --out {o}/dbos.jsonl", {}),
             (f"{MULTI} {m} --framework temporal --seed 409 --out {o}/temporal.jsonl", {}),
             (f"{MULTI} {m} --framework langgraph-loop --seed 410 --out {o}/langgraph-loop.jsonl", {})]
    return cmds


def host() -> list[tuple]:
    h = f"{V2}/host"
    return [(f"-m benchmarks.crashstudy.hostcrash --courier on --latency 3 --think 0.2 --trials 20 --seed 501 --out {h}/courier.jsonl", {}),
            (f"-m benchmarks.crashstudy.hostcrash --courier on --durable --latency 3 --think 0.2 --trials 20 --seed 502 --out {h}/courier-durable.jsonl", {}),
            (f"-m benchmarks.crashstudy.hostcrash --courier off --latency 3 --think 0.2 --trials 20 --seed 503 --out {h}/no-courier.jsonl", {})]


def real_task() -> list[tuple]:
    """A code review, eight files, the agent building each prompt from its notes."""
    env = {"CS_TASK": "1"}
    o = f"{V2}/task/gemini-3.5-flash-lite"
    m = ("--via google --model gemini-3.5-flash-lite --framework agentos --task --calls 8 "
         "--think 0.5 --max-tokens 200 --trials 10")
    cmds = [(f"{MULTI} {m} --crash agent --adopt no --seed 801 --out {o}/agent-adopt-no.jsonl", {}),
            (f"{MULTI} {m} --crash agent --adopt yes --seed 802 --out {o}/agent-adopt-yes.jsonl", {}),
            (f"{MULTI} {m} --courier off --seed 803 --out {o}/courier-off.jsonl", {}),
            (f"{MULTI} {m} --courier on --seed 804 --out {o}/courier-on.jsonl", {})]
    s = f"{V2}/task/standin"
    r = ("-m benchmarks.crashstudy.run --frameworks agentos --ignore-keys --timeout 300 --keyed no "
         "--calls 8 --latency 2 --think 0.5 --calibrate 3 --trials 30")
    cmds += [(f"{r} --crash agent --adopt no --seed 811 --out {s}/agent-adopt-no.jsonl", env),
             (f"{r} --crash agent --adopt yes --seed 812 --out {s}/agent-adopt-yes.jsonl", env),
             (f"{r} --courier off --seed 813 --out {s}/courier-off.jsonl", env),
             (f"{r} --courier on --seed 814 --out {s}/courier-on.jsonl", env),
             (f"{r} --courier contained --seed 815 --out {s}/courier-contained.jsonl", env)]
    return cmds


def namespace() -> list[tuple]:
    """A container-style stop: the stopped-VM arms, with the VM left running."""
    o = f"{V2}/namespace"
    c = "-m benchmarks.crashstudy.hostcrash --kill namespace --latency 3 --think 0.2 --trials 20"
    return [(f"{c} --courier on --seed 511 --out {o}/courier.jsonl", {}),
            (f"{c} --courier off --seed 512 --out {o}/no-courier.jsonl", {}),
            # The reply kept outside the container, with the runtime's own keys.
            (f"{c} --courier off --keyed --seed 513 --out {o}/keys-no-gateway.jsonl", {}),
            (f"{c} --courier off --keyed --gateway --seed 514 --out {o}/gateway-no-courier.jsonl", {}),
            (f"{c} --courier on --keyed --gateway --seed 515 --out {o}/gateway-courier.jsonl", {})]


def model_route() -> list[tuple]:
    """The kernel's own model routing, which every other AgentOS arm goes around: stand-in."""
    o = f"{V2}/model/windows"
    r = f"{RUN} --frameworks agentos --ignore-keys --keyed no --model-path --trials 30 --timeout 300 --calibrate 3"
    return [(f"{r} --crash agent --adopt no --seed 901 --out {o}/agent-adopt-no.jsonl", {}),
            (f"{r} --crash agent --adopt yes --seed 902 --out {o}/agent-adopt-yes.jsonl", {}),
            (f"{r} --courier off --seed 903 --out {o}/courier-off.jsonl", {}),
            (f"{r} --courier on --seed 904 --out {o}/courier-on.jsonl", {}),
            (f"{r} --courier contained --seed 905 --out {o}/courier-contained.jsonl", {}),
            (f"{r} --crash agent --drift tail --seed 906 --out {o}/drift-agent-tail.jsonl", {}),
            (f"{r} --courier on --drift tail --seed 907 --out {o}/drift-courier-tail.jsonl", {})]


def model_route_real() -> list[tuple]:
    """The same route to a real model, with exact counts from Groq's quota header."""
    o, m = f"{V2}/model/allam", "--model allam-2-7b --frameworks agentos --keyed no --trials 20 --calibrate 3"
    env = {"CS_MODEL_PATH": "1"}
    return [(f"{REAL} {m} --crash agent --adopt no --out {o}/agent-adopt-no.jsonl", env),
            (f"{REAL} {m} --crash agent --adopt yes --out {o}/agent-adopt-yes.jsonl", env),
            (f"{REAL} {m} --courier off --out {o}/courier-off.jsonl", env),
            (f"{REAL} {m} --courier on --out {o}/courier-on.jsonl", env),
            (f"{REAL} {m} --courier contained --out {o}/courier-contained.jsonl", env)]


def signals_study() -> list[tuple]:
    """Do the attempt fields each framework documents change for a re-executed call?"""
    return [(f"{RUN} --frameworks temporal,dbos,langgraph-node,prefect --ignore-keys --keyed no --trials 30 "
             f"--calibrate 3 --seed 1001 --out {V2}/signals/standin.jsonl", {"CS_SIGNALS": "1"})]


def drift_frameworks() -> list[tuple]:
    """Every prompt changes after a restart: does a recorded reply answer the new one?
    The -arg adapters pass the prompt into the durable unit; the others build it there."""
    fws = "temporal-arg,dbos-arg,prefect-arg,langgraph-func,langgraph-node,crewai,crewai-checkpoint,autogen"
    d = f"{V2}/drift-frameworks"
    return [(f"{RUN} --frameworks {fws} --ignore-keys --keyed no --trials 20 --calibrate 3 --drift all "
             f"--seed 1011 --out {d}/frameworks.jsonl", {}),
            (f"{RUN} --frameworks agentos --ignore-keys --keyed no --trials 20 --calibrate 3 --drift all "
             f"--courier on --no-fingerprint --seed 1012 --out {d}/agentos-nofp.jsonl", {}),
            (f"{RUN} --frameworks agentos --ignore-keys --keyed no --trials 20 --calibrate 3 --drift all "
             f"--courier on --seed 1013 --out {d}/agentos.jsonl", {}),
            # The control: the same adapters, the same crashes, the prompt unchanged.
            (f"{RUN} --frameworks temporal-arg,dbos-arg,prefect-arg,langgraph-func --ignore-keys --keyed no "
             f"--trials 20 --calibrate 3 --seed 1014 --out {d}/unchanged.jsonl", {}),
            # The same engines with the prompt built inside the durable unit.
            (f"{RUN} --frameworks temporal,dbos --ignore-keys --keyed no --trials 20 --calibrate 3 --drift all "
             f"--seed 1015 --out {d}/in-unit.jsonl", {})]


def long_calls(L: str, seed: int) -> list[tuple]:
    """Calls long enough that recovery usually finds the courier still at work."""
    o = f"{V2}/long/L{L}"
    r = (f"{RUN} --frameworks agentos --ignore-keys --keyed no --calls 3 --think 0.2 --latency {L} --calibrate 3 "
         f"--timeout 600")
    env = {"CS_TOOL_TIMEOUT": "300"}
    return [(f"{r} --trials 40 --courier on --seed {seed} --out {o}/courier-on.jsonl", env),
            (f"{r} --trials 20 --courier off --seed {seed + 1} --out {o}/courier-off.jsonl", env),
            (f"{r} --trials 20 --courier contained --seed {seed + 2} --out {o}/courier-contained.jsonl", env),
            (f"{r} --trials 40 --crash agent --adopt yes --seed {seed + 3} --out {o}/agent-adopt-yes.jsonl", env),
            (f"{r} --trials 20 --crash agent --adopt no --seed {seed + 4} --out {o}/agent-adopt-no.jsonl", env)]


def cache_baseline(mode: str, L: str, seed: int) -> list[tuple]:
    """The simple baseline: a response cache outside the crash, no key and no courier."""
    fws = "agentos,langgraph-node,dbos" if L == "3" else "agentos,langgraph-node,temporal,dbos,prefect,autogen"
    extra = {"CS_ACTIVITY_TIMEOUT": "30"} if L == "3" else {}
    return [(f"{RUN} --frameworks {fws} --ignore-keys --keyed no --trials 20 --calibrate 3 --latency {L} "
             f"--cache-proxy {mode} --seed {seed} --out {V2}/cache/{mode}-L{L}.jsonl", extra)]


def real_long(via: str, model: str, slug: str, words: int) -> list[tuple]:
    """Real calls of ten seconds or more, on free tiers: sends, the mechanisms and their baselines."""
    o, m = f"{V2}/long/real/{slug}", (f"--via {via} --model {model} --calls 3 --think 0.2 --words {words} "
                                      f"--max-tokens {words * 3} --calibrate 2 --gap 4")
    return [(f"{MULTI} {m} --trials 15 --framework agentos --courier on --seed 1101 --out {o}/courier-on.jsonl", {}),
            (f"{MULTI} {m} --trials 10 --framework agentos --courier off --seed 1102 --out {o}/courier-off.jsonl", {}),
            (f"{MULTI} {m} --trials 10 --framework agentos --courier contained --seed 1103 --out {o}/courier-contained.jsonl", {}),
            (f"{MULTI} {m} --trials 15 --framework agentos --crash agent --adopt yes --seed 1104 --out {o}/agent-adopt-yes.jsonl", {}),
            (f"{MULTI} {m} --trials 10 --framework agentos --crash agent --adopt no --seed 1105 --out {o}/agent-adopt-no.jsonl", {})]


def service() -> list[tuple]:
    """AgentOS as a systemd service, its main process killed."""
    o = f"{V2}/service"
    c = "-m benchmarks.crashstudy.hostcrash --kill service --latency 3 --think 0.2 --trials 20"
    return [(f"{c} --courier on --kill-mode control-group --seed 1041 --out {o}/control-group-courier.jsonl", {}),
            (f"{c} --courier off --kill-mode control-group --seed 1042 --out {o}/control-group-no-courier.jsonl", {}),
            (f"{c} --courier on --kill-mode mixed --seed 1043 --out {o}/mixed-courier.jsonl", {}),
            (f"{c} --courier on --kill-mode process --seed 1044 --out {o}/process-courier.jsonl", {})]


def kernel_kill() -> list[tuple]:
    """The kernel's process alone dies, as in an out-of-memory kill."""
    o = f"{V2}/kernel-kill"
    r = f"{RUN} --frameworks agentos --ignore-keys --keyed no --crash kernel --trials 30 --calibrate 3"
    return [(f"{r} --courier on --seed 1051 --out {o}/courier-on.jsonl", {}),
            (f"{r} --courier off --seed 1052 --out {o}/courier-off.jsonl", {})]


def wsl_path(p: Path) -> str:
    """This checkout as WSL sees it, for the Linux stage."""
    return f"/mnt/{p.drive[0].lower()}{p.as_posix()[2:]}"


LINUX = r"""set -e
cd ~/aos
rm -rf agentos
cp -r @ROOT@/agentos .
cp @ROOT@/benchmarks/crashstudy/*.py benchmarks/crashstudy/
find . -name __pycache__ -type d -prune -exec rm -rf {} +
python3 -m pip install --user --break-system-packages psutil >/dev/null 2>&1 || true
run() { python3 -m benchmarks.crashstudy.run --frameworks agentos --ignore-keys --timeout 300 "$@"; }
O=v2
mkdir -p $O
for spec in "agent-adopt-no|--crash agent --adopt no|701" "agent-adopt-yes|--crash agent --adopt yes|702" \
            "courier-off|--courier off|703" "courier-on|--courier on|704" "courier-contained|--courier contained|705"; do
  IFS='|' read -r name flags seed <<< "$spec"
  run --keyed both --trials 30 --calibrate 5 $flags --seed $seed --out $O/$name.jsonl
done
run --keyed no --trials 30 --calibrate 5 --crash agent --drift tail --seed 711 --out $O/drift-agent-tail.jsonl
run --keyed no --trials 30 --calibrate 5 --courier on --drift tail --seed 712 --out $O/drift-courier-tail.jsonl
run --keyed no --trials 30 --calibrate 3 --courier on --double 3 --seed 713 --out $O/double-courier-on.jsonl
python3 -m benchmarks.crashstudy.costs --runs 10 --frameworks agentos --out $O/costs.jsonl
D=@ROOT@/benchmarks/crashstudy/results/v2/linux
mkdir -p $D
cp $O/*.jsonl $D/
"""


LINUX_MODEL = r"""set -e
cd ~/aos
rm -rf agentos
cp -r @ROOT@/agentos .
cp @ROOT@/benchmarks/crashstudy/*.py benchmarks/crashstudy/
find . -name __pycache__ -type d -prune -exec rm -rf {} +
O=v2-model
mkdir -p $O
for spec in "agent-adopt-no|--crash agent --adopt no|911" "agent-adopt-yes|--crash agent --adopt yes|912" \
            "courier-off|--courier off|913" "courier-on|--courier on|914" "courier-contained|--courier contained|915"; do
  IFS='|' read -r name flags seed <<< "$spec"
  python3 -m benchmarks.crashstudy.run --frameworks agentos --ignore-keys --keyed no --model-path --trials 30 \
      --timeout 300 --calibrate 3 $flags --seed $seed --out $O/$name.jsonl
done
D=@ROOT@/benchmarks/crashstudy/results/v2/model/linux
mkdir -p $D
cp $O/*.jsonl $D/
"""


LINUX_TESTING = r"""set -e
cd ~/aos
rm -rf agentos tests
cp -r @ROOT@/agentos @ROOT@/tests .
cp @ROOT@/benchmarks/crashstudy/*.py benchmarks/crashstudy/
find . -name __pycache__ -type d -prune -exec rm -rf {} +
python3 -m benchmarks.crashstudy.barriers --trials 15 --seed 601 --out v2-barriers
python3 -m benchmarks.crashstudy.mutants --trials 10 --seed 701 --out v2-mutants
python3 -m benchmarks.crashstudy.mutants --trials 10 --seed 751 --out v2-mutants \
    --long-calls clean,running-courier-taken-as-unsent,courier-lock-not-held
D=@ROOT@/benchmarks/crashstudy/results/v2
mkdir -p $D/barriers $D/mutants
cp v2-barriers/*.jsonl $D/barriers/
cp v2-mutants/summary.jsonl v2-mutants/long-calls.jsonl $D/mutants/
"""


LINUX_V9 = r"""set -e
cd ~/aos
rm -rf agentos tests
cp -r @ROOT@/agentos @ROOT@/tests .
cp @ROOT@/benchmarks/crashstudy/*.py benchmarks/crashstudy/
find . -name __pycache__ -type d -prune -exec rm -rf {} +
D=@ROOT@/benchmarks/crashstudy/results/v2
run() { python3 -m benchmarks.crashstudy.run --frameworks agentos --ignore-keys --keyed no "$@"; }
mkdir -p v9/long v9/kernel-kill
for L in 10 30; do
  r="--calls 3 --think 0.2 --latency $L --calibrate 3 --timeout 600"
  CS_TOOL_TIMEOUT=300 run $r --trials 40 --courier on --seed 11$L --out v9/long/L$L-courier-on.jsonl &
  CS_TOOL_TIMEOUT=300 run $r --trials 20 --courier contained --seed 12$L --out v9/long/L$L-courier-contained.jsonl &
  CS_TOOL_TIMEOUT=300 run $r --trials 40 --crash agent --adopt yes --seed 13$L --out v9/long/L$L-agent-adopt-yes.jsonl &
done
wait
for c in on off; do
  run --crash kernel --trials 30 --calibrate 3 --courier $c --seed 1061 --out v9/kernel-kill/courier-$c.jsonl
done
mkdir -p $D/long/linux $D/kernel-kill/linux
cp v9/long/* $D/long/linux/
cp v9/kernel-kill/* $D/kernel-kill/linux/
"""

LINUX_MUTANTS = r"""set -e
cd ~/aos
rm -rf agentos tests
cp -r @ROOT@/agentos @ROOT@/tests .
cp @ROOT@/benchmarks/crashstudy/*.py benchmarks/crashstudy/
find . -name __pycache__ -type d -prune -exec rm -rf {} +
python3 -m benchmarks.crashstudy.mutants --trials 10 --seed 771 --out v2-mutants --longer \
    --long-calls clean,running-courier-taken-as-unsent,courier-lock-not-held
cp v2-mutants/longer-calls.jsonl @ROOT@/benchmarks/crashstudy/results/v2/mutants/
"""


def plan() -> list[tuple[str, dict[str, list[tuple]]]]:
    return [
        ("signals", {"signals": signals_study()}),
        ("drift-frameworks", {"drift-frameworks": drift_frameworks()}),
        # Long calls are almost all waiting, so the arms run side by side.
        ("long", {f"L{L}-{n}": [c] for L, seed in (("10", 1021), ("30", 1031))
                  for n, c in enumerate(long_calls(L, seed))}),
        ("kernel-kill", {"kernel-kill": kernel_kill()}),
        ("real-long", {"gemini": real_long("google", "gemini-3.5-flash-lite", "gemini-3.5-flash-lite", 1500),
                       "cohere": real_long("cohere", "command-r7b-12-2024", "cohere-command-r7b", 700)}),
        ("cache", {f"{m}-{L}": cache_baseline(m, L, seed) for m, L, seed in
                   (("plain", "0.3", 1071), ("coalesce", "0.3", 1072), ("plain", "3", 1073), ("coalesce", "3", 1074))}),
        ("linux-v9", {"linux-v9": [("LINUX_V9", {})]}),
        ("service", {"service": service()}),
        ("mutants-long", {"mutants-long": [("LINUX_MUTANTS", {})]}),
        ("stand-in", {"stand-in": stand_in()}),
        ("models", {
            "allam": groq_allam(),
            "gpt-oss-120b": groq_other("openai/gpt-oss-120b", "gpt-oss-120b"),
            "qwen": groq_other("qwen/qwen3.8-27b", "qwen3.8-27b"),
            "cohere": elsewhere("cohere", "command-r7b-12-2024", "cohere-command-r7b"),
            "google": elsewhere("google", "gemini-3.5-flash-lite", "gemini-3.5-flash-lite"),
            # A free model throttled until resumes timed out (1 valid trial of 11),
            # so its crash arms were dropped on 3 Nov. Gemini's free daily quota ran
            # out after its AgentOS arms: the frameworks there need a fresh day.
            "bazaarlink": elsewhere("bazaarlink", "qwen/qwen3.7-flash:free", "bazaarlink-qwen3.7-flash",
                                    probe_only=True),
        }),
        ("linux", {"linux": [("LINUX", {})]}),
        ("host", {"host": host()}),
        ("task", {"task": real_task()}),
        ("namespace", {"namespace": namespace()}),
        ("model", {"model": model_route()}),
        ("model-linux", {"model-linux": [("LINUX_MODEL", {})]}),
        ("model-real", {"model-real": model_route_real()}),
        ("testing", {"testing": [("LINUX_TESTING", {})]}),
    ]


def load_keys(path: str) -> dict[str, str]:
    out = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        m = re.match(r"\s*(?:export\s+)?([A-Z_]+)=(.*)$", line)
        if m:
            out[m.group(1)] = m.group(2).strip().strip("'\"")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("--keys", required=True)
    ap.add_argument("--state", required=True, help="directory for markers and logs")
    ap.add_argument("--only", help="comma-separated stages")
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()
    state = Path(args.state)
    state.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":  # no sleep while this runs
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | 0x00000001)
    env = dict(os.environ, **load_keys(args.keys), PYTHONPATH=str(ROOT), PYTHONUTF8="1")
    py = args.python
    log_lock = threading.Lock()

    def say(text: str) -> None:
        with log_lock, open(state / "progress.log", "a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {text}\n")

    def group(name: str, cmds: list[tuple]) -> None:
        for n, (cmd, extra) in enumerate(cmds):
            mark = state / f"{name}-{n:03d}.done"
            if mark.exists():
                continue
            log = state / f"{name}-{n:03d}.log"
            for attempt in range(2):
                say(f"{name} {n + 1}/{len(cmds)} start: {cmd[:150]}")
                t0 = time.time()
                with open(log, "a", encoding="utf-8") as fh:
                    if cmd in ("LINUX", "LINUX_MODEL", "LINUX_TESTING", "LINUX_V9", "LINUX_MUTANTS"):
                        script = {"LINUX": LINUX, "LINUX_MODEL": LINUX_MODEL, "LINUX_TESTING": LINUX_TESTING,
                                  "LINUX_V9": LINUX_V9, "LINUX_MUTANTS": LINUX_MUTANTS}[cmd]
                        rc = subprocess.run(["wsl", "-d", "Ubuntu", "--exec", "bash", "-lc",
                                             script.replace("@ROOT@", wsl_path(ROOT))],
                                            stdout=fh, stderr=fh).returncode
                    else:
                        out = re.search(r"--out (\S+)", cmd)
                        if out:
                            (ROOT / out.group(1)).parent.mkdir(parents=True, exist_ok=True)
                        full = [py] + cmd.split()
                        if " -m benchmarks.crashstudy.run " in f" {cmd} " or "realmain" in cmd \
                                or "multi" in cmd:
                            full += ["--python", py]
                        rc = subprocess.run(full, cwd=ROOT, env=dict(env, **extra),
                                            stdout=fh, stderr=fh).returncode
                say(f"{name} {n + 1}/{len(cmds)} rc={rc} in {(time.time() - t0) / 60:.1f} min")
                if rc == 0:
                    mark.touch()
                    break

    only = set(args.only.split(",")) if args.only else None
    for stage, groups in plan():
        if only and stage not in only:
            continue
        if args.list:
            print(stage, {k: len(v) for k, v in groups.items()})
            continue
        say(f"== stage {stage}")
        threads = [threading.Thread(target=group, args=g) for g in groups.items()]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    say("ALL DONE")
    return 0


if __name__ == "__main__":
    sys.exit(main())
