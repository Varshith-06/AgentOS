# What a crash costs

One workload, seven agent frameworks, each recovered its own documented way,
killed at random moments, with charges counted by a provider none of them can
touch.

```
pip install langgraph langgraph-checkpoint-sqlite temporalio dbos crewai prefect autogen-core
python -m benchmarks.crashstudy.run --trials 30 --out results/standin/main.jsonl
python -m benchmarks.crashstudy.analyze results/standin/main.jsonl
```

`--python` runs the framework processes under a different interpreter from the
harness, which helps when one of them doesn't install on yours. CrewAI's
dependencies have no wheels for Python 3.14; the published results use 3.13.

## The pieces

| file | what it is |
|---|---|
| `paidapi.py` | the provider: a separate process that charges on receipt, answers after `--latency` seconds, and honours `Idempotency-Key` |
| `client.py` | the one call every framework makes, plus the local "think" work before it |
| `adapters.py` | the workload written for each framework, in that framework's documented style, with its documented resume path |
| `run.py` | starts a run, kills the whole process tree at a uniform random moment after the first charge, resumes, reads the ledger |
| `analyze.py` | tables, Wilson and bootstrap intervals, the analytic model, figures |
| `realprovider.py` | the same questions against a real inference API (Groq): is a request whose caller died still admitted, is `Idempotency-Key` honoured, and the end-to-end crash run with every call a real completion |
| `realmain.py` | the whole main run again with every call a real Groq completion, one process per model, waiting out the daily quota |
| `otherproviders.py` | the provider probes on OpenRouter (generation records say whether a request was cancelled) and Google Gemini (response ids) |
| `khan_crewai_probe.py` | an in-process copy of a published CrewAI `from_checkpoint` case, to check it against our two-process result |
| `summary.py` | every number the paper quotes from the real-model runs, the fix, and the gateway, and the main results table |
| `reproduce.sh` | runs all of the analyses above on the committed results and lists the commands that re-run the experiments |

## The results

| folder | experiments |
|---|---|
| `results/standin/` | the main run (`main.jsonl`), the think-time sweep (`sweep-T*.jsonl`), 12-call runs (`n12.jsonl`), the Temporal/DBOS signal run (`detect.jsonl`), CrewAI 1.15.2 (`crewai-1.15.2.jsonl`), and their ledgers |
| `results/real/` | the main run on three Groq models (`real-main-<model>.jsonl`), the sweep on `allam-2-7b` (`real-sweep-T*.jsonl`), and the provider probes: Groq (`real-inflight`, `real-admission`, `real-idem`, `real-e2e`), OpenRouter (`real-or-*`), Gemini (`real-gemini-idem`) |
| `results/agentos-fix/` | AgentOS's adoption and couriers: `fix-*.jsonl` on the stand-in under Windows, `linux/fix-*.jsonl` under Linux, `real-fix-*.jsonl` on `allam-2-7b` |
| `results/gateway/` | the other frameworks with keys, without (`gateway-none.jsonl`, `real-gw-allam-off.jsonl`) and with the gateway (`gateway.jsonl`, `real-gw-allam-on.jsonl`) |

Each `.jsonl` row is one crash. A row names its ledger, and a real run keeps a
folder per trial (`<file>/<framework>/<run>/calls.jsonl`: every send and
reply), so a folder can be moved as a whole. Framework-internal state is not
kept (see `results/.gitignore`).

## What counts

The ledger, and nothing else. A **duplicate** is a call index charged more
than once. A trial **fails** if any index is never charged or the resume does
not exit cleanly. Trials that finish before their kill time, or never make a
first charge, are recorded with that status rather than dropped.

A kill that lands more than 50 ms after its drawn time means the harness froze
along with the run (a suspend, or the process tree being paused). The trial is
recorded as `late`, a resumed run draws it again, and `analyze.py` leaves it
out: its kill time is no longer a sample of the uniform draw, and a call frozen
in flight can outlive its own timeout and fail the run for reasons that have
nothing to do with the crash.

## Holding things equal

- Every framework does its local work *before* the call, inside the same
  durable unit as the call. Re-running that work is free, so a duplicate can
  only come from the call itself.
- The kill takes the whole process tree at once: a job object on Windows, a
  process-group `SIGKILL` on POSIX. A framework's child processes cannot
  finish a reply on its behalf.
- Configurations are interleaved within each round, so drift in machine load
  spreads across all of them.
- With `CS_KEYED=1` each call carries a key derived from whatever stable
  identity that framework offers for it (`adapters.py` says which). AgentOS
  derives its own.

## Could the application have known?

Each row also keeps `signals`: what the framework told the re-executed call
about itself. That's the attempt number Temporal and DBOS hand to an activity
or step (recorded via `client.note`), and AgentOS's `OutcomeAmbiguous`
events. `analyze.py` prints, per framework, how many duplicates came with a
signal and how many signals came without one. LangGraph, CrewAI, Prefect and
AutoGen document no per-call signal that would distinguish a re-execution after
a crash.
Rows written before signals were recorded were backfilled from the run
directories where those still existed (AgentOS rows); Temporal's and DBOS's
signals come from the dedicated `detect.jsonl` run.

`reproduce.sh` regenerates every number, table, and figure in the paper from
the committed results, and lists the commands that re-run the experiments.

## Against a real provider

```
GROQ_API_KEY=... python -m benchmarks.crashstudy.realprovider inflight --trials 40
GROQ_API_KEY=... python -m benchmarks.crashstudy.realprovider idem --trials 10
GROQ_API_KEY=... python -m benchmarks.crashstudy.realprovider e2e --framework langgraph-node --trials 25
python -m benchmarks.crashstudy.realprovider summary
```

Groq does not expose a per-request bill on the free tier, but every response
says how long until the model's daily request quota is full again
(`x-ratelimit-reset-requests`). That grows by exactly `86400/limit` seconds per
admitted request while shrinking in real time, so the difference between two
tiny probe requests counts exactly what the provider admitted in between, as
long as no gap between requests outlasts one refill interval (86.4 s at 1000 a
day). A longer gap loses one request from the count, so every row records its
largest gap (`max_gap`) and the analyses drop rows where it exceeds `refill`. A 429 is refused, not admitted, and is excluded. Each experiment uses
its own model, because quotas are per model and a concurrent run on the same
model would be counted too.

In `e2e` mode the framework adapters call Groq instead of `paidapi.py`
(`CS_PROVIDER=groq`). The client logs every send and reply, so the analysis
knows which call was outstanding when the kill landed.

## The model

For a recovery unit of one call, a kill duplicates a charge only if it lands
between the charge and the recorded reply. That happens with probability
`N·L / (L + (N-1)(T+L))`, which tends to `L/(T+L)`. For a recovery unit that
spans the run, every call charged so far repeats. `analyze.py` prints both
predictions next to the measurements.

Real frameworks take a little longer than `T+L` per call. Given the run's
ledger (`--ledger`), `analyze.py` also measures each framework's median call
cycle from consecutive charges and evaluates the model at it, and splits kills
into those inside a call and those after its reply, to show how long each
framework takes to record a reply it has already received.

## The fix

`run.py` and `realmain.py` take the AgentOS mechanisms as flags.
`--crash agent` kills only the agent's process and leaves the kernel running.
`--adopt yes|no` lets the kernel finish a dead agent's call, or re-issue it.
`--courier off|on|contained` carries each call in a courier process outside the
runtime's process tree. `contained` keeps the couriers inside it, so they die
with the runtime as they would on a host crash, which is the control.
`--ignore-keys` makes the stand-in behave like the real providers, and
`--gateway` puts `agent gateway` between the frameworks and the provider. The
commands are at the bottom of `reproduce.sh`.
