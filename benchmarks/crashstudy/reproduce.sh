#!/usr/bin/env sh
# Every number, table, and figure in the paper, from the committed results.
#   sh benchmarks/crashstudy/reproduce.sh [out-dir]
# Run from the repository root. To re-run the experiments themselves, see the
# commands at the bottom (about 4 hours on one machine, plus Groq quota).
set -e
OUT=${1:-paper}
R=benchmarks/crashstudy/results
PY=${PYTHON:-python}
A="$PY -m benchmarks.crashstudy.analyze"

echo "== main run: Table II, Fig. 2, in-flight window, visibility"
$A $R/main.jsonl --ledger $R/main-ledger.db --latex $OUT/results-table.tex --bars $OUT/fig-bars.pdf

echo "== think-time sweep: Fig. 3"
$A $R/main.jsonl $R/sweep-T0.jsonl $R/sweep-T0.3.jsonl $R/sweep-T0.9.jsonl \
   --ledger $R/main-ledger.db --sweep $OUT/fig-sweep.pdf
$A $R/main.jsonl $R/sweep-T0.jsonl $R/sweep-T0.3.jsonl $R/sweep-T0.9.jsonl | tail -n 3

echo "== N = 12"
$A $R/n12.jsonl --ledger $R/n12-*-ledger.db

echo "== what the application can see (Temporal, DBOS)"
$A $R/detect.jsonl | tail -n 4

echo "== real provider (Groq), Section VII"
$PY -m benchmarks.crashstudy.realprovider summary

# The experiments (each resumes if interrupted; finished trials are skipped):
#   $PY -m benchmarks.crashstudy.run --trials 30 --seed 2026 --out $R/main.jsonl
#   for T in 0 0.3 0.9; do $PY -m benchmarks.crashstudy.run --frameworks agentos,langgraph-node,dbos \
#       --keyed no --trials 20 --think $T --out $R/sweep-T$T.jsonl; done
#   $PY -m benchmarks.crashstudy.run --frameworks langgraph-node,langgraph-loop,crewai,dbos \
#       --keyed no --trials 20 --calls 12 --out $R/n12.jsonl
#   $PY -m benchmarks.crashstudy.run --frameworks temporal,dbos --keyed no --trials 30 --out $R/detect.jsonl
#   GROQ_API_KEY=... $PY -m benchmarks.crashstudy.realprovider inflight --model openai/gpt-oss-20b --trials 40 --window 1.8
#   GROQ_API_KEY=... $PY -m benchmarks.crashstudy.realprovider inflight --model qwen/qwen3.8-27b --trials 20 --window 1.8
#   GROQ_API_KEY=... $PY -m benchmarks.crashstudy.realprovider idem --model openai/gpt-oss-20b --trials 10
#   GROQ_API_KEY=... $PY -m benchmarks.crashstudy.realprovider idem --model qwen/qwen3.8-27b --trials 10
#   GROQ_API_KEY=... $PY -m benchmarks.crashstudy.realprovider e2e --model openai/gpt-oss-120b --framework langgraph-node --trials 25
#   GROQ_API_KEY=... $PY -m benchmarks.crashstudy.realprovider e2e --model qwen/qwen3.8-27b --framework dbos --trials 25
