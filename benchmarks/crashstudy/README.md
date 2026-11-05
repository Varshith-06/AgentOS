# What a crash costs

One workload, six agent frameworks and AgentOS, each recovered its own documented way,
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
| `cacheproxy.py` | the simple baseline: a response cache outside the crash, keyed on a hash of the request |
| `summary_v3.py` | signals, changed prompts, long calls, a service manager, the kernel alone, and the baseline |
| `summary.py` | every number the paper quotes from the real-model runs, the fix, and the gateway, and the main results table |
| `reproduce.sh` | runs all of the analyses above on the committed results and lists the commands that re-run the experiments |

## The results

| folder | experiments |
|---|---|
| `results/standin/` | the main run (`main.jsonl`), the think-time sweep (`sweep-T*.jsonl`), 12-call runs (`n12.jsonl`), the Temporal/DBOS signal run (`detect.jsonl`), CrewAI 1.15.2 (`crewai-1.15.2.jsonl`), and their ledgers |
| `results/real/` | the main run on three Groq models (`real-main-<model>.jsonl`), the sweep on `allam-2-7b` (`real-sweep-T*.jsonl`), and the provider probes: Groq (`real-inflight`, `real-admission`, `real-idem`, `real-e2e`), OpenRouter (`real-or-*`), Gemini (`real-gemini-idem`) |
| `results/v2/` | what the paper reports for AgentOS, re-run after three recovery fixes: the mechanisms (`windows/fix`, `linux`, `real`, `multi`), changed prompts, harder crashes, costs, the eight-file review (`task`), the kernel model route (`model`), a stopped VM (`host`), and a killed PID namespace (`namespace`). `summary_v2` reads these |
| `results/agentos-fix/` | superseded October runs of the same mechanisms, kept for the record |
| `results/gateway/` | the other frameworks with keys, without (`gateway-none.jsonl`, `real-gw-allam-off.jsonl`) and with the gateway (`gateway.jsonl`, `real-gw-allam-on.jsonl`) |

The Linux stand-in rows in `results/v2/linux` and `results/v2/model/linux` have their ledgers
beside them, and all 540 kept rows recount the same from those ledgers.

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
signal and how many signals came without one. CrewAI and AutoGen document no
such signal that we found.

With `CS_SIGNALS=1` the adapters also read every field the other frameworks
document (`results/v2/signals`). LangGraph's `node_attempt` and Prefect's `run_count`
stayed at 1 on every re-executed call. DBOS's `recovery_attempts` rose on every
one, and on the calls after them too: it says the workflow recovered, not which
call ran twice.

## A recorded reply and a changed prompt

`--drift all` changes every prompt after a restart (`results/v2/drift-frameworks`,
with the same crashes and unchanged prompts in `unchanged.jsonl` as the control). The
`temporal-arg`, `dbos-arg`, `prefect-arg` and `langgraph-func` adapters build the
prompt outside the durable unit and pass it in, and check each reply where it lands.
`in-unit.jsonl` has Temporal and DBOS with the prompt built inside the unit, where a
completed call is skipped, so a zero holds by construction. Temporal's documentation
lists an Activity's input among the changes that are safe to make, DBOS asks a workflow to
invoke the same steps with the same inputs, and LangGraph asks that values that change
between attempts stay inside tasks. What the run measures is that none of them reports a
mismatch. In `temporal-arg` the prompt comes from a module passed through the workflow
sandbox.

`--drift all` is the worst case: every prompt differs in each life, from a number unique
to the process. Kill times come from uncrashed runs, except in the earliest stand-in runs
and the VM and namespace runs, which use the run's nominal length. `results/gateway/` also
holds 138 `never-started` CrewAI rows that failed at start-up, before any kill.

## Long calls, service managers, the kernel alone, and the baseline

- `results/v2/long`: 10 and 30 s calls, three to a run, with `fates` on each row saying
  what recovery found (`running` is the wait for a courier still at work). The arms
  ran side by side, since long calls are almost all waiting.
- `results/v2/long/real`: long replies on free tiers, three calls to a run, counted as sends.
  On Cohere's `command-r7b` the calls took a median of 13 s; its trial key's monthly limit of
  1,000 calls ended the contained arm at 6 crashes, before the adoption arms ran. Two copies of
  the run overlapped after a restart, each writing its own trials. Gemini's long requests
  returned server errors throughout, so none was kept and that set is not relied on.
- `results/v2/service`: `hostcrash.py --kill service` runs AgentOS as a systemd user
  service and kills its main process. systemd's default kills the rest of the control
  group, couriers included. Couriers survive under `KillMode=process`, which systemd's
  documentation discourages; otherwise keep the reply in a gateway outside the service.
- `results/v2/kernel-kill`: `--crash kernel` kills the kernel's process alone.
- `results/v2/cache`: `--cache-proxy plain|coalesce` puts a response cache outside the
  crash, with no key and no courier. It merges every identical request, including one
  the caller meant to make twice. Coalescing makes a repeat wait for a call still out.
Rows written before signals were recorded were backfilled from the run
directories where those still existed (AgentOS rows); Temporal's and DBOS's
signals come from the dedicated `detect.jsonl` run.

`reproduce.sh` regenerates Table III and Fig. 3 of the paper from the committed
results, prints the counts behind its other results, and lists the commands that
re-run the experiments. Pooled figures in the paper are sums of those counts. Run
it from the repository root.

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

The recorded gateway arms ran under `--on-drift replay`, which was the gateway's default
then and is still the default of `run.py`, so the commands here reproduce them as they ran.
The gateway-outside-a-namespace arms (`--kill namespace --keyed --gateway` in `hostcrash.py`) use
`agent gateway` directly, under its current default. `agent gateway` itself now defaults to `reissue`. None of the 3,512 requests in those arms
changed its body, so the counts do not depend on the policy.

## A real task

`CS_TASK=1` replaces "reply with the number" with a code review of eight AgentOS
source files. Reading a file is the agent's tool step, and the agent builds each
prompt from that file and its own notes, so each prompt depends on every earlier
reply and passes through the request fingerprint. `run_v2 --only task` runs it, and
`summary_v2` reports it from `results/v2/task/`.

The 150-word arms in `results/v2/real/allam/long-*` are not relied on either: the
model did not always begin with the number, so the reply check failed in about half
their runs, with or without the mechanisms.

It replaces the chained-prompt arms in `results/v2/real/allam/chain-*`, which are
kept but not relied on. They built the prompt in the process making the call, so
under couriers, with a new process per call, no prompt carried the previous reply.
The prompt hashes in those arms' `calls.jsonl` show this.

## Studies and experiment labels

"Re-billed" in file names and script output means admitted or sent again: no bill
was observed. The paper names seven studies. File names and script output here still use the older labels:

| Study in the paper | Labels here |
|---|---|
| Framework survey | E1, E2, E4 |
| Window sweep | E3, the baseline arms of E11 |
| Provider probes | E5 |
| Mechanism evaluation | E6, E7 |
| Limits | the rest of E11, E9, E10, E13, E14, the kernel model route, with E8 (gateway) and E12 (costs) |
| Step kills | `barriers.py` |
| Seeded bugs | `mutants.py` |

## The adapters, and why each is built this way

All of them are in `adapters.py`, with `temporal_defs.py` and `autogen_defs.py`, about 500 lines
together. Each uses the recovery mechanism its framework documents, at the finest unit the framework
offers, since a coarser unit only repeats more. If you think one is unfair, this is the place to look.

| Configuration | Function | Unit and mechanism | Resume | Why, and what it follows |
|---|---|---|---|---|
| LangGraph, node per call | `langgraph_node` | One node per call, `SqliteSaver` | `invoke(None)` on the same thread | The finest unit, and the [documented](https://docs.langchain.com/oss/python/langgraph/persistence) resume |
| LangGraph, loop in one node | `langgraph_loop` | One node holding the loop | Same | Deliberately coarse: what a whole-run unit costs |
| Temporal, activity per call | `temporal` | One activity per call, retried by the server; timeout set by `CS_ACTIVITY_TIMEOUT` | A new worker | The [documented](https://docs.temporal.io/activity-definition) unit. The timeout is sized to the slowest call, so slow replies are not retried |
| Temporal, split workers | `temporal_split` | Workflow and activity on separate workers | Workers restarted | A common deployment; Section V-C of the paper |
| DBOS, step per call | `dbos` | `@DBOS.step`, no automatic retries, SQLite system database | `DBOS.launch()` | The [documented](https://docs.dbos.dev/architecture) recovery. `current_attempt` applies only to [steps with automatic retries](https://docs.dbos.dev/python/reference/contexts), so it stays empty here |
| CrewAI Flow + `@persist` | `crewai` | `@persist` on a flow | `kickoff(inputs={"id": ...})` | [Documented](https://docs.crewai.com/en/concepts/flows); it re-runs from `@start`, so it is a whole-run unit |
| CrewAI, guarded steps | `crewai_guarded` | The same, each step skipping itself if restored state shows it done | Same | Our variant: the least a developer can add to get a per-call unit |
| CrewAI, `CheckpointConfig` | `crewai_checkpoint` | A [checkpoint](https://docs.crewai.com/en/concepts/checkpointing) after each method | `Flow.from_checkpoint` | The other documented mechanism |
| Prefect, cached tasks | `prefect` | `INPUTS` [cache policy](https://docs.prefect.io/v3/concepts/caching), [persisted results](https://docs.prefect.io/v3/advanced/results) | Re-run the flow | Completed tasks return from the cache |
| AutoGen, `save_state` per call | `autogen` | `save_state` after each call | `load_state`, continue | AutoGen has no durable execution; this is its [documented state API](https://microsoft.github.io/autogen/stable/reference/python/autogen_core.html) |

Adapters for LangGraph and Prefect do not read those frameworks' attempt counters, and the DBOS adapter
does not read the attempt count in its workflow status, as the paper says.

## Checking the paper's numbers

It needs Python 3.11 or later and matplotlib (`pip install matplotlib`), and none of the frameworks.
Some result paths are long, so on Windows clone to a short path:
`git clone -c core.longpaths=true https://github.com/Varshith-06/AgentOS.git C:\agentos`.

`reproduce.sh` regenerates Table III and Fig. 3, prints the counts behind Tables IV to VI, the step kills and
the seeded bugs, and ends with `claims.py`, which recomputes the numbers the paper states only in its text
and exits non-zero if any differ. It calls no provider. Exact reruns of the experiments differ, since kills
are random and calls take real time; the commands are at the bottom of `reproduce.sh`.

## Testing the harness

`barriers.py` kills a run at a named protocol step, through hooks in the harness, at 22
combinations of step, victim, and mechanism setting. Each row in its `STEPS` holds the
repeat and flag the design predicts. `mutants.py` seeds ten recovery bugs in throwaway
copies of AgentOS and runs one battery against each and a clean copy, recording which check
fires: duplicate, completeness, reply, flag, or the test modules that script crashes. The
bugs are hand-written and the battery was built knowing them, so the counts show that some
bugs need a given check, not how strong it is. `summary_v2` reports both from
`results/v2/barriers/` and `results/v2/mutants/`. Both need Linux and no API key.

`couriers-long-calls` in `long-calls.jsonl` was added after the battery. All its copies use
one seed, so its kills fall at the same moments, and the duplicate in two of them is the
same kill, about 0.3 s into its call against at least 1.9 s for the other nine.

## The kernel model route

Every other AgentOS arm makes its API call through a kernel tool driver. `--model-path`
(`CS_MODEL_PATH=1` on a real model) sends it through the kernel's own model routing
instead. `run_v2 --only model,model-linux,model-real` runs it, and `summary_v2` reports it
from `results/v2/model/`. On this route the kernel or a courier makes the HTTP call, so on
a real model the agent logs the send and the reply, and in-flight counts are approximate.

The candidate sets `rate_limit_retries`, as `_groq` waits out a 429 on the tool path.
Without it agents stopped on a 429 and the run finished early, which the harness then
counted as a trial, so a first run on `allam-2-7b` was discarded. It now marks such a run
`incomplete` and draws it again.
