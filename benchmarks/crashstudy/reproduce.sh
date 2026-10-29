#!/usr/bin/env sh
# Every number, table, and figure in the paper, from the committed results.
#   sh benchmarks/crashstudy/reproduce.sh [out-dir]
# Run from the repository root. Nothing here calls a provider; the commands
# that re-run the experiments themselves are at the bottom.
set -e
OUT=${1:-paper}
R=benchmarks/crashstudy/results
S=$R/standin
PY=${PYTHON:-python}
A="$PY -m benchmarks.crashstudy.analyze"
mkdir -p "$OUT"

echo "== E1 main run, stand-in: in-flight window, visibility (Table III's left half)"
$A $S/main.jsonl --ledger $S/main-ledger.db

echo "== E3 think-time sweep: Fig. 3, with the real-model points overlaid"
$A $S/main.jsonl $S/sweep-T0.jsonl $S/sweep-T0.3.jsonl $S/sweep-T0.9.jsonl \
   --ledger $S/main-ledger.db --sweep "$OUT/fig-sweep.pdf" \
   --real $R/real/real-sweep-T0.jsonl $R/real/real-sweep-T0.1.jsonl \
          $R/real/real-sweep-T0.3.jsonl $R/real/real-sweep-T0.9.jsonl | tail -n 3

echo "== E4 what the application can see (Temporal, DBOS)"
$A $S/detect.jsonl | tail -n 4

echo "== CrewAI 1.15.2 from_checkpoint (the release Khan tested)"
$A $S/crewai-1.15.2.jsonl | tail -n 2

echo "== E5 Groq probes"
$PY -m benchmarks.crashstudy.realprovider summary

echo "== E2, E3, E5-E8: real models, the fix, the gateway (Table III, Table IV)"
$PY -m benchmarks.crashstudy.summary --latex "$OUT/results-table.tex"

# The experiments (each resumes if interrupted; finished trials are skipped).
# Stand-in runs take hours on one machine; real runs wait out Groq's free
# quota (about 1000 requests a day per model, 7000 for allam-2-7b).
#   $PY -m benchmarks.crashstudy.run --trials 30 --seed 2026 --out $S/main.jsonl
#   for T in 0 0.3 0.9; do $PY -m benchmarks.crashstudy.run --frameworks agentos,langgraph-node,dbos \
#       --keyed no --trials 20 --think $T --out $S/sweep-T$T.jsonl; done
#   $PY -m benchmarks.crashstudy.run --frameworks temporal,dbos --keyed no --trials 30 --out $S/detect.jsonl
#   for M in openai/gpt-oss-20b openai/gpt-oss-120b qwen/qwen3.8-27b; do   # one process per model
#       GROQ_API_KEY=... CS_ACTIVITY_TIMEOUT=30 $PY -m benchmarks.crashstudy.realmain --model $M --trials 10; done
#   for T in 0 0.1 0.3 0.9; do GROQ_API_KEY=... $PY -m benchmarks.crashstudy.realmain --model allam-2-7b \
#       --frameworks agentos,langgraph-node,dbos --keyed no --trials 20 --think $T \
#       --out $R/real/real-sweep-T$T.jsonl; done
#   GROQ_API_KEY=... $PY -m benchmarks.crashstudy.realprovider inflight --model openai/gpt-oss-20b --trials 40 --window 1.8
#   GROQ_API_KEY=... $PY -m benchmarks.crashstudy.realprovider idem --model openai/gpt-oss-20b --trials 10
#   OPENROUTER_API_KEY=... $PY -m benchmarks.crashstudy.otherproviders or-inflight --trials 18
#   OPENROUTER_API_KEY=... $PY -m benchmarks.crashstudy.otherproviders or-idem --trials 10
#   GEMINI_API_KEY=... $PY -m benchmarks.crashstudy.otherproviders gemini-idem --trials 10
# The fix (E6 on Windows, E7 on Linux): the stand-in ignores keys, as real providers do.
#   F=$R/agentos-fix   # on Linux: F=$R/agentos-fix/linux
#   for A in no yes; do $PY -m benchmarks.crashstudy.run --frameworks agentos --keyed both --ignore-keys \
#       --crash agent --adopt $A --trials 30 --out $F/fix-agent-adopt-$A.jsonl; done
#   for C in off on contained; do $PY -m benchmarks.crashstudy.run --frameworks agentos --keyed both \
#       --ignore-keys --courier $C --calibrate 2 --trials 30 --out $F/fix-courier-$C.jsonl; done
#   for A in no yes; do GROQ_API_KEY=... $PY -m benchmarks.crashstudy.realmain --model allam-2-7b \
#       --frameworks agentos --crash agent --adopt $A --trials 10 --out $F/real-fix-agent-adopt-$A.jsonl; done
#   for C in off on contained; do GROQ_API_KEY=... $PY -m benchmarks.crashstudy.realmain --model allam-2-7b \
#       --frameworks agentos --courier $C --trials 10 --out $F/real-fix-courier-$C.jsonl; done
# The gateway (E8), keyed, other frameworks:
#   $PY -m benchmarks.crashstudy.run --keyed yes --ignore-keys --trials 30 --out $R/gateway/gateway-none.jsonl
#   $PY -m benchmarks.crashstudy.run --keyed yes --ignore-keys --gateway --trials 30 --out $R/gateway/gateway.jsonl
#   GROQ_API_KEY=... $PY -m benchmarks.crashstudy.realmain --model allam-2-7b --keyed yes --trials 10 \
#       [--gateway] --out $R/gateway/real-gw-allam-{off,on}.jsonl
