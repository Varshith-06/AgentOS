# AgentOS

![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)
![Dependencies: zero](https://img.shields.io/badge/dependencies-zero-brightgreen)
![Tests: 184 passing](https://img.shields.io/badge/tests-184%20passing-brightgreen)
![Model: gpt--oss--120b](https://img.shields.io/badge/default%20model-gpt--oss--120b-orange)

**An operating system for AI agents — a kernel, not a framework.**

Agents run as OS processes with PIDs, a nine-state lifecycle, and a
scheduler. Every tool call, model call, and message crosses a syscall
boundary, is permission-checked, journaled, and billed. `kill -9` the runtime
mid-task and it resumes with **zero work re-executed and zero calls
re-billed** — measured, not claimed, against LangGraph, CrewAI, AutoGen, and
Temporal on identical workloads.

And because an agent's identity is JSON, agents don't have to be written in
advance. POST a sentence and a tool list; a planner **invents the team at
runtime** — roles, tools, event wiring, all of it — while the kernel holds
one line: *nothing in that tree, at any depth, can ever reach a capability
you didn't grant at the door.*

```
$ curl -H "Authorization: Bearer $TOKEN" -X POST localhost:7070/task \
    -d '{"goal": "perform an experiment about trees", "tools": ["filesystem"]}'

$ agent ps
PID  NAME      STATUS    PERMS       EVENTS
1    Planner   Running   filesystem  -
2    Surveyor  Running   filesystem  pub:MeasurementsReady
3    Analyst   Waiting   -           pub:AnalysisReady sub:MeasurementsReady
```

Nobody wrote a `Surveyor` class. Nobody drew a graph. The planner invented
both agents, granted one of them `filesystem` (a subset of its own grant —
the kernel refuses anything wider), and made up the event names that wire
them together. The runtime schedules, journals, meters, and recovers all of
it like any other process.

Zero dependencies. Stdlib only. Everything — kernel, daemon, tests,
benchmarks — runs offline against a deterministic mock model; point the
config at a real one and the same code runs live.

**Contents:** [Runtime, not library](#a-runtime-not-a-library) ·
[The numbers](#the-numbers) · [Head-to-head](#head-to-head) ·
[How it works](#how-it-works) · [Invented agents](#agents-invented-at-runtime) ·
[Running it for real](#running-it-for-real) · [Try it](#try-it) ·
[Layout](#layout)

---

## At a glance

| | |
|---|---|
| **Crash recovery** | the only runtime measured that repeats **0** work after a hard kill |
| **Approval latency** | 1.2ms, approve → agent finished — lowest of the five |
| **Durable step overhead** | 3.4ms — lowest of the five |
| **Multi-app cost ledger** | one ledger, exact to the token, across every application |
| **Capability ceiling** | a task's root grant bounds its whole invented tree, kernel-enforced |
| **Auth** | bearer token on every route; refuses to bind non-loopback without one |
| **Test suite** | 184 tests, zero dependencies, fully offline |

---

## A runtime, not a library

LangGraph, CrewAI, and AutoGen are libraries: you `import` them, and your
application owns the orchestration state. AgentOS is a **daemon your
applications connect to** — closer to Postgres than to an ORM. That is a real
trade, in both directions.

**What the runtime shape buys:**

- **Durability nobody has to write.** Every syscall is journaled; a crashed
  task resumes from its last completed call with nothing re-executed and no
  model call billed twice. In a library, checkpointing is your application's
  problem, at whatever granularity you remembered to implement.
- **One view of everything.** Every application's agents in one `agent ps`,
  one dependency graph, one event timeline, one cost ledger exact to the
  token. "What did all of this cost today?" has an answer.
- **Enforcement, not convention.** A library's permission check lives in the
  same process as the agent that wants around it. Here the kernel refuses the
  call in a different process — the agent physically cannot reach a tool it
  wasn't granted, and cannot grant a child what it doesn't hold.
- **Agents outlive callers.** Submit and disconnect; the work continues. A
  human approves a blocked agent from another terminal, days later, even
  across a runtime restart.
- **An ops surface.** `ps / top / logs / events / kill / pause / resume /
  approve / grant / revoke`, plus a live dashboard — for agents, which is
  where fleets of them get opaque.

**What it costs:**

- **A moving part.** A server to start, monitor, and upgrade, where a library
  is a `pip install`. For one app with one workflow in one process, a
  library is genuinely simpler.
- **The boundary is strict.** Agent params must survive `json.dumps` — no
  closures, no live objects — and agent classes must be importable by the
  daemon. That discipline is exactly what makes recovery and process
  isolation work, but it is a discipline.
- **Ecosystem.** LangGraph has hundreds of integrations, streaming UIs, and a
  hosted platform. This has six drivers, four model providers, and no
  streaming. Zero dependencies cuts both ways.
- **One box, one writer.** A single kernel over SQLite. It profiles
  disk-bound at ~3.4ms/step, so one machine absorbs a lot of agents whose
  real work is model calls — but there is no failover and no horizontal
  scale.

---

## The numbers

`python benchmarks/bench.py` — offline, deterministic, mock models, 10ms
tick. Timings are the **best of several runs on an otherwise-idle machine**,
the usual convention for measuring a runtime's own cost: background load only
ever makes a run slower, so the fastest one is the closest estimate of what
the code actually costs. A busy machine will read higher.

**A hard kill costs nothing beyond the last completed syscall.** Three agents,
18 steps, killed halfway with no cleanup, recovered:

| metric | result |
|---|---|
| steps re-executed after recovery | **0** |
| journaled syscalls replayed | 24 |
| recovery wall time (replay + all remaining work) | 0.25s |

**Humans wake agents at scheduler speed** — `approve()` to *agent finished*,
through the full path (dependency resolved, re-queued, scheduled, run):

| metric | result |
|---|---|
| median | **1.2ms** |
| worst | 1.6ms |

**One runtime accounts for every application, exactly.** Three apps × five
agents × two model calls against one daemon:

| metric | result |
|---|---|
| throughput | **44.6 agents/s** |
| ledger, 30 concurrent billed calls | $0.001380 — **exact to the token** |

The two *claims* — zero re-execution and an exact ledger — are deterministic
and held in every run, idle machine or not.

---

## Head-to-head

`benchmarks/compare.py` runs identical workloads against **LangGraph 1.2.9**,
**CrewAI 1.15.4**, **AutoGen 0.7.5**, and **Temporal 1.30**. Executions are
counted in a shared tally table — no framework grades its own homework — and
the crash is a real OS kill delivered once every framework has completed the
same work. Any comparator not installed is skipped.

**Recovery — billable calls repeated after a hard kill** (6 calls, killed
after 3):

| framework | repeated | why |
|---|---|---|
| **AgentOS** | **0** | journals each syscall reply as it completes |
| LangGraph (one node per call) | 1 | the in-flight node has no checkpoint |
| Temporal | 1 | at-least-once activity semantics |
| LangGraph (calls in one node) | 3 | a crash re-runs the whole node |
| CrewAI Flows | 3 | `@persist` restores state, then replays from the start |
| AutoGen | 3 | `save_state` restores state; the handler starts over |

The axis is checkpoint granularity. Others persist at node or method
boundaries; AgentOS journals at the syscall, so even the call *in flight* at
the kill returns its recorded reply instead of running twice. LangGraph can
approach this — if you restructure into one node per side effect. AgentOS
gives it to code written the obvious way.

**Overhead — one durable step, no real work:**

| framework | per step |
|---|---|
| **AgentOS** | **3.4ms** |
| AutoGen | 4.1ms |
| LangGraph | 5.4ms |
| CrewAI Flows | 15.2ms |
| Temporal | 68.6ms |

**Human-in-the-loop — approve → finished:**

| framework | median | worst |
|---|---|---|
| **AgentOS** | **1.2ms** | **2.3ms** |
| LangGraph | 3.2ms | 17.7ms |
| Temporal | 6.2ms | 11.0ms |
| CrewAI / AutoGen | — | no durable wait-for-a-human primitive to time |

**Cost under multi-application load** (3 apps × 5 agents × 2 billed calls):

| framework | calls seen | ledger |
|---|---|---|
| **AgentOS** | 30/30 | **one ledger, exact to the token** |
| LangGraph | 30/30 | per-app only |
| CrewAI Flows | 30/30 | per-app only |

Everyone does the work; the difference is who can answer "what did all of
that cost." Libraries can't, structurally: each app owns its own state.

**With real work in the steps** (600ms each, one modest model call), every
framework lands within a few points of the floor — AutoGen +1.5%, LangGraph
+1.7%, **AgentOS +2.4%**, Temporal +2.8%, CrewAI +3.3%. That column is a
wash: the ranking would not survive a different machine, and most of
AgentOS's gap is a Windows timer quantum that largely disappears on Linux.

**On reproducing these.** Absolute figures move a lot with machine state —
across one day the same benchmark gave AgentOS between 3.4ms and 5.7ms per
step, and every comparator moved with it in lockstep. What does *not* move is
the ordering, because `compare.py` measures all five inside one process under
identical conditions. Expect different numbers on your machine; expect the
same ranking. The recovery column is deterministic by construction and was
identical in every run.

Scope, honestly: one machine, one workload family, and Temporal's single
repeat is its documented at-least-once contract working as designed — it
buys multi-host durability AgentOS does not attempt. This measures runtime
overhead and recovery granularity, not ecosystems or agent quality.

---

## How it works

### The one decision everything hangs on

An agent never holds a reference to the kernel, the process table, or
another agent. Its entire world is `Context`, and every call crosses to the
kernel as a JSON-serializable `Syscall`, answered by a `Reply` — enforced at
runtime, not asked politely. That single boundary pays for all three hard
features:

- **Crash recovery** — everything an agent does is JSON crossing a boundary,
  so the kernel journals every reply. Replay hands back recorded answers
  instead of re-executing; the agent fast-forwards to where it died and goes
  live. Every completed syscall is a checkpoint.
- **Real process isolation** — anything that survives `json.dumps` survives a
  socket. The daemon runs each agent as its **own OS process**, syscalls
  crossing a token-authenticated loopback TCP connection as JSON lines (or
  stdio pipes with `--transport pipe`). Not a line of agent code knows which.
- **Invented agents** — an agent whose identity is its parameters can be
  *constructed by a model*, and still journals, recovers, and isolates like
  anything hand-written.

### The kernel

**Processes.** A process table over nine lifecycle states with an enforced
transition table — an illegal move raises instead of corrupting state. Each
agent carries a full card: PID, parent, children, status, priority, model,
permissions, event wiring, memory, cost, checkpoint. Execution slots bound
concurrency; a woken agent re-queues rather than resuming instantly — that's
what makes it a scheduler and not a callback, and a test fails if it
regresses. Policies: FIFO, priority (with ageing), dependency-aware — run
whoever unblocks the most work, a policy only a kernel with the whole
wait-for graph can have. The loop is event-driven with the tick as a ceiling:
a syscall or a newly-runnable agent wakes it in microseconds.

**Events and dependencies.** Agents never call each other — `Context` gives
them no way to. They publish; the runtime wakes subscribers. A waiting agent
declares a dependency *set* — agents, events, timers, human approvals — and
the scheduler wakes it when the last one resolves. A wait that would close a
cycle is refused when requested; a stall with no cycle is detected and
reported; a wait on an event that *provably* nobody will publish fails
immediately — including when its last possible publisher exits mid-wait.
Nothing hangs silently.

**Humans as kernel objects.** `request_approval(role=...)` blocks the agent
on a *durable* approval — a dependency-graph node identical in kind to an
agent or a timer. Kill the runtime while blocked; restart; the agent
re-attaches to the same pending approval instead of asking twice. A human
can approve while nothing is running at all.

**Tools behind capabilities.** Agents never import tool libraries. They
request a capability by name; the kernel checks the permission matrix
*before* dispatch (deny by default, revocation applies to a running system)
and routes to a driver owning timeouts, rate limits, retries, caching, and
error handling. Six ship: filesystem (sandboxed to a root), shell, python,
sql, http, browser. Every dispatch is recorded — the runtime knows all tool
usage, and shows it.

**Memory.** Six kinds behind four verbs: working and scratchpad die with the
process; shared crosses agents through the kernel with an access list;
longterm and semantic are keyed by agent *name* and survive restarts;
episodic is the kernel's own record, read-only. The semantic embedding is a
deterministic stdlib placeholder a real model can replace without any agent
changing.

**Models by capability class.** Agents ask for `"fast"` or `"reasoning"`,
never a model name. The default chain is **gpt-oss-120b** four ways — Groq,
OpenRouter, local Ollama, then an offline mock that always answers:

```jsonc
// .agentos/models.json — set GROQ_API_KEY or OPENROUTER_API_KEY and the
// same agent code lands on the real model; unset it and the mock answers.
"fast": [
  {"provider": "openai", "base_url": "https://api.groq.com/openai/v1",
   "api_key_env": "GROQ_API_KEY", "model": "openai/gpt-oss-120b", ...},
  {"provider": "openai", "base_url": "https://openrouter.ai/api/v1",
   "api_key_env": "OPENROUTER_API_KEY", "model": "openai/gpt-oss-120b", ...},
  {"provider": "openai", "base_url": "http://localhost:11434/v1",
   "model": "gpt-oss:120b", ...},
  {"provider": "mock", "model": "mock-fast"}
]
```

Unavailable candidates are skipped, failing ones fall through, and the
runtime can rank by projected cost, latency, or quality (`"prefer":
"cheapest"`). Tokens and dollars are recorded per agent and per model;
failures are on the record too. Providers: any OpenAI-compatible endpoint,
Anthropic, LiteLLM (optional), mock.

**Retries are the scheduler's job.** An agent that raises can be restarted
within a budget, as a real `Failed → Ready` edge in the state machine. The
restarted agent replays its journal *minus the failed tail* — a failed
syscall had no side effect, so it re-executes live instead of replaying its
own recorded failure forever. A killed agent is never retried: a human said
stop.

---

## Agents invented at runtime

Everything above works with agents somebody wrote. The interesting case is
when nobody wrote them.

`LLMAgent` is one class whose **parameters are its identity** — role, goal,
tools, model. Creating an agent at runtime means constructing four JSON
values, so a model can do it, and the result still satisfies every kernel
discipline: it journals, recovers, runs in its own OS process, and shows up
on `agent ps` under the role the model invented. The planner is the same
class with `may_spawn=True`.

Its protocol is one JSON action per model turn — `tool`, `spawn`, `publish`,
`wait`, `remember`/`recall` (the memory system), `ask_human` (the durable
approval object), `done` — with the full `Context` surface reachable, because
a planner that can't use the kernel's services routes around them. Every
decision is logged before it runs; `agent logs` narrates the model's run.

**Authority: the operator's grant is the ceiling.** An agent may delegate
only a subset of what it holds — checked in the kernel at spawn:

```python
await ctx.spawn(LLMAgent(role="Surveyor", ...), grant=["filesystem"])  # ⊆ mine, or refused
```

So the capability set on the *root* agent bounds the entire tree, however
many layers of agents a model invents, whatever it names them. Start a
planner with `["filesystem"]` and nothing beneath it reaches a shell. You
cannot answer "what could this touch?" by reading code that doesn't exist
yet — so the kernel answers it instead. (Grants ride on the *process*, not
the class name; a narrowed child is never re-widened; authority dies with
its holder.)

**Coordination: the parent names the events.** Loose coupling doesn't relax
just because the agents are invented — but somebody has to choose event
names, and no programmer is present. So the parent chooses, per child, at
spawn:

```json
{"action": "spawn", "role": "Analyst", "tools": [],
 "publishes": ["AnalysisReady"], "subscribes": ["MeasurementsReady"]}
```

The Surveyor and the Analyst never name each other; the runtime does the
waking. Because the parent named *both sides* of every match, the kernel
knows the vocabulary — so publishing an unwired name is refused with a
message the model can act on, and waiting for a name nobody will publish
fails fast instead of hanging. Attenuation is for security; wiring is for
correctness. Publishing an event harms nobody — declarations just turn a
silent stall into a loud error.

See it offline: `python -m agentos.cli run examples/planner.py` (the model
is a deterministic script, so what it demonstrates is the runtime).

**It runs live.** With `GROQ_API_KEY` set, the same `POST /task` path drives
real **gpt-oss-120b** — no code change, the router simply stops falling
through to the mock:

```
POST /task  "perform a small experiment about trees: invent measurements,
             save them to a file, and state one conclusion"
         -> Finished in 2.1s

decided: tool: filesystem.write     # the model chose the tool and the path
decided: done: finishing
FileCreated  from the filesystem driver

tree_measurements.txt:
  Tree 1: Height=12.5m, TrunkDiameter=0.45m, LeafCount=3400
  Tree 2: Height=8.3m,  TrunkDiameter=0.30m, LeafCount=2100  ...

result: "...trees with larger trunk diameters tend to have higher leaf
         counts, suggesting a correlation between trunk size and foliage."

ledger: 2 calls, 1111+554 tokens, $0.000582, 0.85s mean latency
```

Real model, real tool call, real file, metered to six decimal places — and
the write landed inside the sandbox root the operator configured, because the
grant said `filesystem` and nothing else. The model solved that task solo
rather than delegating; multi-agent delegation under a live model is the next
thing to exercise.

---

## Running it for real

### The hosted path

```bash
AGENTOS_TOKEN=$(openssl rand -hex 16) \
python -m agentos.cli daemon --host 0.0.0.0 --task-tools filesystem,http
```

```bash
curl -H "Authorization: Bearer $AGENTOS_TOKEN" -X POST host:7070/task \
     -d '{"goal": "...", "tools": ["filesystem"], "priority": "High", "retries": 1}'
curl -H "Authorization: Bearer $AGENTOS_TOKEN" host:7070/task/1   # result + the team
```

Authority is bounded twice: `--task-tools` is the most a submitted task may
ever hold; the request asks for a subset; attenuation carries it down the
tree. Everything off the socket is validated, clamped, or refused — goal
length, tool names, step and child limits.

**Auth:** bearer token on every route — there are no exempt reads, because
`/ps` carries other applications' results. Constant-time compare; denials
reveal nothing; nothing logs the token. No token = loopback only: binding
any other interface unauthenticated is refused at startup rather than
allowed as a typo (`--insecure` is the explicit escape hatch for a proxy
that already authenticates). Local clients pick the token up from
`.agentos/daemon.json` automatically; remote ones read `AGENTOS_TOKEN`; the
dashboard takes `/?token=…` once and uses a header thereafter.

### Can this run 24/7 on a company server, working on your codebase?

Mostly yes — with clear eyes about what it is and isn't. What's true today:
the daemon runs indefinitely, survives hard kills (`--recover` resumes
mid-task work with nothing repeated), authenticates its API, meters every
token spent, and can take a sandboxed `filesystem` root pointed at a
repository plus `http` for the outside world. An internal team can POST
tasks from CI, cron, or chat hooks and read results — that is a real,
defensible deployment.

What to be honest about before leaving it unattended:

- **Spending is metered, not capped.** The ledger is exact; nothing yet
  *enforces* a per-task or per-day budget. Watch it, or add the cap first.
- **`shell` and `python` grants are arbitrary code execution** by design.
  The permission system decides *whether* those run; nothing confines what
  they do once granted. Confinement is the container you run the daemon in
  — drop privileges, mount the repo read-only where you can, and don't put
  `shell` in `--task-tools` for anything the outside world can reach.
- **One box.** No failover, no horizontal scale, SQLite underneath. Fine for
  an internal workload; not an SLA.
- **Single tenant.** One namespace, one ledger, one process table — a team's
  shared visibility, not customer isolation.
- **Something must submit the work.** There's no cron inside; pair it with
  whatever already schedules things in your shop.

### Origin

The project began as a design document — `AgentOS.pdf`, still in the repo —
whose eight-phase plan is fully implemented and audited, and whose claims
the benchmarks above measure rather than assert. What grew past the
document: runtime-invented agents, capability attenuation, parent-named
event wiring, the task API, and authentication.

---

## Try it

No installs, no API keys — deterministic agents, so a bug reproduces the
same way twice:

```bash
python -m unittest discover tests -v          # 184 tests

python -m agentos.cli run examples/tree.py --slots 2      # processes + scheduling
python -m agentos.cli run examples/pipeline.py            # events + dependencies
python -m agentos.cli run examples/deadlock.py            # neither stall mode hangs
python -m agentos.cli run examples/deploy.py              # human approval
python -m agentos.cli run examples/finance.py             # tools + permissions
python -m agentos.cli run examples/memory.py              # six memory kinds
python -m agentos.cli run examples/assistant.py           # model routing
python -m agentos.cli run examples/crash.py               # kill -9 it, then:
python -m agentos.cli recover                             # nothing runs twice
python -m agentos.cli run examples/planner.py             # the invented team

python -m agentos.cli daemon                              # the shared runtime
python examples/app_research.py                           # app 1, another terminal
python examples/app_support.py                            # app 2, another terminal
# dashboard: http://127.0.0.1:7070/

python benchmarks/bench.py                                # the numbers above
python benchmarks/compare.py                              # vs the field
```

Watch any run from a second terminal:

```bash
python -m agentos.cli top          # live process table
python -m agentos.cli ps           # the full per-agent card
python -m agentos.cli wait 3       # block until pid 3 terminates
python -m agentos.cli events -v    # who published what, and whom it woke
python -m agentos.cli logs         # every transition and every model decision
python -m agentos.cli kill 3       # kill a child; the parent survives
python -m agentos.cli approve 1 --as "Senior Engineer"
python -m agentos.cli grant Finance sql
python -m agentos.cli revoke Finance sql   # applies to a running system
```

---

## Layout

```
agentos/
  kernel/     states.py process.py scheduler.py messages.py store.py
              events.py depgraph.py permissions.py memory.py models.py
              gpu.py kernel.py
  drivers/    base.py              # timeout / rate limit / retry / cache, once
              filesystem.py shell.py python.py sql.py http.py browser.py
  runtime/    executor.py          # asyncio executor; owns Context
              subproc.py child.py  # agents as OS processes; TCP or stdio syscalls
              daemon.py            # the shared runtime that outlives applications
  api/        server.py            # HTTP control plane: auth, /task, /agents
              dashboard.py         # live dashboard served at /
  agents/     base.py              # Agent, the direct-invocation guard, spec loader
              llm.py               # LLMAgent: params are the identity
  client.py                        # RuntimeClient: submit / task / wait / ps
  cli.py                           # agent ps top wait logs events approvals tools
                                   #   kill pause resume approve grant revoke
                                   #   recover daemon
examples/     tree pipeline deadlock deploy finance memory assistant crash
              planner              # a sentence in, an invented team out
              app_research app_support software_company research_assistant
              customer_support
benchmarks/   bench.py             # recovery, approval latency, multi-app cost
              compare.py           # vs LangGraph / CrewAI / AutoGen / Temporal
tests/        184 of them          # including what the API and kernel refuse
```
