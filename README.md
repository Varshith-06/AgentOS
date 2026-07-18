# AgentOS

![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)
![Dependencies: zero](https://img.shields.io/badge/dependencies-zero-brightgreen)
![Tests: 152 passing](https://img.shields.io/badge/tests-152%20passing-brightgreen)
![Status: all phases complete](https://img.shields.io/badge/status-complete-blue)

An operating system-inspired runtime for autonomous AI agents. Linux abstracts
hardware; AgentOS abstracts intelligence. Agents are processes, not objects:
the runtime owns their lifecycle, scheduling, dependencies, memory, tools, and
recovery.

Zero dependencies, stdlib only. Everything below — the kernel, the daemon, the
examples, the test suite, the benchmark — runs offline, with no installs and no
API keys. (The one exception is the optional cross-framework comparison in
`benchmarks/compare.py`, which needs the other frameworks installed to have
anything to compare against, and skips whichever are absent.) Design doc:
`AgentOS.pdf`.

**Contents:** [What ships](#what-ships) · [Results](#results) ·
[Compared to the field](#compared-to-the-field) ·
[The one architectural decision](#the-one-architectural-decision) ·
[Components](#components) · [Try it](#try-it) · [Layout](#layout)

---

## What ships

- A kernel with a process table, a 9-state agent lifecycle, and swappable
  scheduling policies (FIFO, priority, dependency-aware)
- An event bus and dependency graph with deadlock detection — agents coordinate
  without ever holding a reference to each other
- Human approval as a durable kernel object, not a callback
- Capability-based tool access behind a permission matrix, with six drivers
- Six kinds of memory behind four verbs
- Model routing by capability class, with an exact per-agent cost ledger
- Journal-based crash recovery: every completed syscall is a checkpoint
- A shared runtime daemon that outlives applications, runs each agent as a
  real OS process with syscalls carried over a token-authenticated loopback
  TCP socket (or stdio pipes), and serves a live dashboard
- A CLI (`agent ps / top / wait / events / logs / kill / pause / resume /
  approve / grant / revoke / recover / daemon`), 152 tests, three full example
  applications, a benchmark that measures the design's claims, and a
  head-to-head against LangGraph, CrewAI, AutoGen, and Temporal

### At a glance

| | |
|---|---|
| **Crash recovery** | the only runtime measured that repeats **0** work after a hard kill |
| **Approval latency** | 1.2ms median — lowest of the five |
| **Durable step overhead** | 3.7ms — lowest of the five |
| **Realistic workloads** | +2.3%, within a few points of every comparator |
| **Multi-app cost ledger** | exact to the token across concurrent applications |
| **Test suite** | 152 tests, zero dependencies, fully offline |

---

## Results

`benchmarks/bench.py` measures the three claims from the design doc instead of
asserting them. Offline and deterministic: mock models, 10ms scheduler tick,
task isolation (so the numbers measure the kernel's scheduling and
accounting, with no process-spawn noise). Numbers below are from a full run
on this machine:

**1. A hard kill costs nothing beyond the last completed syscall.** Three
agents run an 18-step workload; the kernel is killed with no cleanup roughly a
third of the way in (3 steps done), then recovered.

| metric | result |
|---|---|
| journaled syscalls replayed | 24 |
| steps re-executed after recovery | **0** |
| recovery wall time (replay + remaining work) | 0.26s |
| every agent finished | yes |

**2. Humans wake agents at scheduler speed.** An agent blocks on
`request_approval`; the time from `approve()` to the agent *finished* — woken,
re-queued, scheduled, run to completion:

| metric | result |
|---|---|
| median (5 rounds) | **1.4ms** |
| worst | 1.6ms |

**3. One runtime accounts for every application, exactly.** Three applications
submit five agents each to one shared daemon; every agent makes two model
calls.

| metric | result |
|---|---|
| throughput | **41.8 agents/s** (15 agents, 0.36s wall) |
| ledger total (30 mock calls) | $0.001380 |
| ledger exact to the token | **yes** |

Reproduce all of it:

```bash
python -m unittest discover tests -v    # 152 tests
python benchmarks/bench.py              # the three tables above
```

---

## Compared to the field

`benchmarks/compare.py` runs identical workloads against **LangGraph 1.2.9**,
**CrewAI 1.15.4** (Flows), **AutoGen 0.7.5**, and **Temporal 1.30** — on one
machine, with SQLite durability wherever the framework offers it and no
network in the loop. Executions are counted in a shared tally table rather
than any framework's own logs, and the crash is a real OS `kill` of a real
process delivered once every framework has completed the same work. Nothing
is simulated in-process.

```bash
pip install langgraph langgraph-checkpoint-sqlite crewai autogen-core temporalio
python benchmarks/compare.py       # any comparator not installed is skipped
```

### 1. Recovery — billable calls repeated after a hard kill

Six billable calls (each an irreversible write plus 80ms of work), killed
after three, then resumed. The number is how many of the six executed
*twice*: work redone, and in a real system, model spend paid twice.

| framework | repeated | why |
|---|---|---|
| **AgentOS** | **0** | journals each syscall reply as it completes |
| LangGraph (one node per call) | 1 | the in-flight node has no checkpoint |
| Temporal | 1 | at-least-once activity semantics |
| LangGraph (calls in one node) | 3 | a crash re-runs the whole node |
| CrewAI Flows | 3 | `@persist` restores state, then replays from `@start` |
| AutoGen | 3 | `save_state` restores state; the handler starts over |

The axis is checkpoint granularity. LangGraph and CrewAI persist at method or
node boundaries, so a crash inside one re-runs all of it; decomposing into one
node per side effect narrows LangGraph to a single repeat, which is the same
place Temporal lands. AgentOS journals the reply the moment a syscall
completes, so the replayed agent is handed the recorded answer and the tool
never fires again — the in-flight case included.

### 2. Overhead — one durable step, no real work

| framework | per step |
|---|---|
| **AgentOS** | **3.7ms** |
| AutoGen | 4.6ms |
| LangGraph | 5.4ms |
| CrewAI Flows | 16.3ms |
| Temporal | 68.6ms |

### 3. Human-in-the-loop — approve → the agent has finished

| framework | median | worst |
|---|---|---|
| **AgentOS** | **1.2ms** | **1.4ms** |
| LangGraph | 3.3ms | 4.3ms |
| Temporal | 8.7ms | 8.8ms |
| CrewAI / AutoGen | — | no durable wait-for-a-human primitive to time |

### 4. Cost under multi-application load

The third axis Phase 8 asks for, and the one that is not really a race. Three
independent applications, five agents each, two billed calls per agent:

| framework | wall | calls seen | ledger |
|---|---|---|---|
| **AgentOS** | 0.34s | 30/30 | **one ledger, exact to the token** |
| LangGraph | 0.29s | 30/30 | per-app only |
| CrewAI Flows | 0.50s | 30/30 | per-app only |

Everyone does the work; the difference is who can answer "what did all of that
cost". AgentOS bills through one kernel, so one ledger covers every
application submitting to it. LangGraph and CrewAI are libraries — each
application owns its own state, and totalling across them is the caller's job.
That is the p.8 shared-runtime claim rather than a performance result, which
is why the wall-time column is close and uninteresting.

### 5. The same steps with real work in them

Bare-step overhead is the wrong denominator for agent systems: a step that
does nothing is not a step anyone runs. The same 30 steps with 600ms of work
each — one modest model call — against an 18.0s floor:

| framework | wall | over floor |
|---|---|---|
| AutoGen | 18.23s | +1.3% |
| LangGraph | 18.36s | +2.0% |
| **AgentOS** | **18.41s** | **+2.3%** |
| Temporal | 18.59s | +3.3% |
| CrewAI Flows | 18.76s | +4.2% |

### What this shows

AgentOS has the lowest per-step overhead, the lowest approval latency, the
only single ledger across applications, and is the only runtime measured here
that repeats **no** work after a crash. On realistic workloads every framework
lands within a few points of the floor and AgentOS is third by 1.0pp — close
enough that the ranking in that column would not survive a different machine.
Most of that gap is one Windows artifact: `ctx.sleep` is a kernel timer, and
`loop.call_later` cannot resolve below the platform's ~15.6ms quantum, where
the comparators' inline `time.sleep` can. On Linux it largely disappears.


## The one architectural decision

Agents live behind a **strict message-passing boundary**. An agent never
holds a reference to the kernel, the process table, or another agent — its
entire world is `Context` (`spawn`, `sleep`, `wait`, `log`, …), and every
call crosses to the kernel as a JSON-serializable `Syscall`, coming back as a
`Reply`. That constraint is enforced at runtime (`assert_serializable`), and
it is what pays for the two hardest features:

- **Crash recovery**: everything an agent does crosses the syscall boundary
  as JSON, so the kernel can journal every reply and replay it after a crash.
- **Execution-substrate freedom**: anything that survives `json.dumps`
  survives a pipe or a socket, so *where* an agent executes is pure
  configuration — no agent code knows or changes.

### Where agents execute: isolation

"Agents are processes" means the **kernel's** sense of process: a PID, an
entry in the process table, a 9-state lifecycle, admission into bounded
execution slots, membership in the dependency graph, and a syscall journal.
What executes the agent's code underneath is the `isolation` setting:

| `isolation=` | what runs the agent | concurrency | default for |
|---|---|---|---|
| `"process"` | its own OS process — own interpreter, own GIL, own address space | true parallelism across cores | the daemon (`agent daemon`) |
| `"task"` | an asyncio task inside the kernel's event loop | cooperative interleaving, one core | embedded `Kernel(...)`, tests, examples, `agent run` |

The two executors (`runtime/subproc.py` and `runtime/executor.py`) present an
identical interface; the kernel cannot tell which one it is driving, and
slots, pause-at-syscall, kill, and journal replay behave identically on both.
Task isolation exists because it is deterministic and cheap — the tests and
the benchmark use it so a bug reproduces the same way twice. Process
isolation is the deployment mode: two agents with disjoint dependencies
execute simultaneously on different cores, and `agent kill` terminates a real
OS process.

To be precise about where asyncio sits in process mode, since "async" and
"parallel" are easy to conflate:

- **In the kernel process**, asyncio is an I/O multiplexer: per agent it runs
  a supervisor task and two pump tasks that shuttle bytes to and from the
  child. No agent code runs here.
- **In each child process**, a private event loop runs exactly one agent.
  The agent is a coroutine not for concurrency — there is nothing to be
  concurrent with — but for *suspension*: `await ctx.anything(...)` is the
  syscall, the point where the agent parks until the kernel's `Reply` arrives
  and the scheduler grants it a slot. This is also what makes recovery
  possible: an agent's resume point is always a syscall boundary, so a
  coroutine never needs to be snapshotted.

### How syscalls travel: transport

With process isolation, `Syscall` and `Reply` cross between child and kernel
as JSON lines over a `transport`:

| `transport=` | channel | default |
|---|---|---|
| `"socket"` | loopback TCP, one connection per agent | **yes** |
| `"pipe"` | the child's stdio | `--transport pipe` to select |

The socket transport works like this: the executor opens one listening socket
on `127.0.0.1` (ephemeral port) the first time it spawns an agent. Each child
is handed the endpoint and a **single-use token** in its environment
(`AGENTOS_CONNECT`, `AGENTOS_TOKEN`), dials back, sends the token as its
first line, and from then on the wire format is byte-identical to the pipe
transport. A connection with an unknown, reused, or missing token is dropped
(there is a test that does exactly this), and stderr still flows over a pipe
for crash diagnostics either way.

The channel is a persistent full-duplex stream deliberately *not* HTTP: a
reply arrives whenever the scheduler grants the agent a slot, not as the
response to a request, so request/response framing is the wrong shape. HTTP
is used where request/response is the right shape — the daemon's control
plane (`api/server.py`), which applications and the CLI talk to.

What the socket transport changes: the syscall channel no longer assumes
parent-child stdio inheritance, which is the prerequisite for agents that
live on other machines or are written in other languages — anything that can
open a TCP connection and speak JSON lines can be an agent. What it does
*not* change today: agents are still spawned locally by the executor, the
listener binds to loopback only, and the stream is plaintext — remote agents
would need the listener opened up plus TLS on the channel, which is future
work, not this commit.

---

## Components

### Processes and scheduling

The kernel keeps a process table over the nine lifecycle states from p.3 —
`Ready`, `Running`, `Waiting`, `Sleeping`, `Blocked`, `Checkpointing`,
`Suspended`, `Finished`, `Failed` — with an enforced transition table: an
illegal move raises `InvalidTransition` rather than quietly corrupting the
process table. Each agent carries the full p.3 metadata card (PID, name,
parent, children, status, priority, waiting-on, **model**, **permissions**,
memory, checkpoint), which is what `agent ps` prints. `spawn`, `kill`,
`pause`, `resume`, and `wait` are kernel operations. Execution
slots bound concurrency: `--slots 2` means only two agents may hold a slot at
once, so a five-agent tree is forced to queue. A woken agent goes back to
`Ready` and re-queues for a slot rather than resuming instantly — that is the
difference between a scheduler and a callback, and there is a test that fails
if it regresses. Scheduling policy is swappable at the command line:
`--policy fifo | priority | dependency`.

Retries are a scheduler responsibility (p.4), not the application's: an agent
that raises can be restarted within a budget (`Kernel(retries=N)`, or a
`retries` attribute on the agent). A restart is a real edge out of `Failed`
back to `Ready`, so `agent logs` narrates `Running → Failed → Ready →
Running → Finished` and the process card counts the attempts. The restarted
agent replays its journal, so a retry costs only the work after its last
completed syscall — the same machinery crash recovery uses, applied to one
process instead of the whole runtime. A *killed* agent is never retried: a
human said stop. Off by default.

The scheduler loop is **event-driven with the tick as a ceiling, not a
period**. A syscall or a newly-runnable agent wakes it in microseconds; `tick`
only bounds how long it may doze when nothing is happening, and rate-limits
the work that reads the outside world (the permission file, the command and
approval tables, the heartbeat, deadlock detection). This matters more than it
sounds: `asyncio.sleep()` cannot resolve below the platform timer quantum
(~15.6ms on Windows), so a loop that slept the tick on every pass paid that
quantum per syscall no matter how small the tick was set. Ticks below the
quantum were an illusion. Being woken by the work itself is what makes a 1ms
and a 20ms tick perform the same today.

### Events and dependencies

In `examples/pipeline.py`, `Research` publishes a fact and stops — it never
mentions `CodeAgent` or `DocumentationAgent`, and does not know they exist:

```python
await ctx.publish("ResearchCompleted", topic=topic, findings=findings)
```

The runtime wakes whoever subscribed. Adding a fourth subscriber required
editing no other agent. That isn't a style rule: `Context` gives an agent no
way to name another agent, and `Agent.run` is wrapped so that calling another
agent's `run()` directly raises `DirectInvocationError`.

Agents wait on a dependency *set*, not a sequence — the scheduler wakes them
when the last one resolves:

```python
result = await ctx.wait_all(agents=[code, docs], events=["HumanApproved"], timer=5)
```

A wait that would close a cycle is refused when it is requested. A stall with
no cycle — everyone `Waiting`, nobody `Sleeping`, no timer pending — is
detected and reported. Neither one hangs (`examples/deadlock.py` exercises
both).

### Human approval

`examples/deploy.py` stops at:

```python
approval = await ctx.request_approval(role="Senior Engineer", reason="Production deployment")
```

`agent ps` shows `Blocked`, waiting on `Senior Engineer`. The human is a node
in the dependency graph — identical in kind to an agent, an event, or a timer —
and

```bash
python -m agentos.cli approve 1 --as "Senior Engineer"
```

wakes the Deployer exactly where it stopped (approving `--as "Intern"` is
refused). An agent blocked on a human is not a deadlock; the runtime keeps
serving.

The approval is a durable kernel object, not a callback. Kill the runtime
while it is blocked, run it again, and the re-run agent re-attaches to the same
pending approval instead of asking twice — `agent approve` writes the grant to
the store, not to the process, so a human can even approve while nothing is
running and the next run sails through. Every grant also publishes a
`HumanApproved` event, so `wait_all(events=["HumanApproved"])` composes with
it.

### Tools and permissions

Agents never import tool libraries. They request a capability by name, and the
kernel dispatches to the driver that owns authentication, rate limiting,
retries, timeouts, and error handling:

```python
rows = await ctx.request_tool("sql", "query", query="SELECT SUM(amount) FROM invoices")
```

The kernel validates the capability against the permission matrix
(`.agentos/permissions.json`, agent name → capabilities, deny by default)
*before* dispatch — the application does not get a vote, and a denial is an
audit-log entry the agent can catch, not a stack trace. In
`examples/finance.py`, the SQL calls run and the browser call is refused.
Revoke with `agent revoke Finance sql` and the same code fails at its first
query — the file is re-read when it changes, so revocation applies to a
*running* system.

Six drivers ship (`agent tools`): filesystem (sandboxed to a root, publishes
`FileCreated`), shell, python (fresh interpreter), sql, http, browser. A
running tool call is a dependency-graph node like any other — the agent shows
`Waiting on tool sql` in `agent ps`, completion publishes `ToolCompleted`, and
the woken agent re-queues for a slot like everyone else.

`drivers/base.py` owns the discipline p.7 assigns to drivers, once, for all of
them: timeouts, rate limiting, retries, error handling, and **caching**.
Caching is opt-in and per-operation, because caching a write would be a
correctness bug rather than an optimisation — each driver declares which of
its ops are reads (`sql.query`, `http.get`, `filesystem.read/list/exists`),
and a TTL turns it on:

```python
Kernel(tools={"http": {"cache_ttl": 30.0}})   # 0, the default, is off
```

Every dispatch is recorded, so the runtime can answer what tools were used and
how often they failed — the p.8 "all tool usage" claim, visible in the
dashboard's Tool usage panel.

### Agents invented at runtime

Everywhere above, an agent is a class someone wrote in advance. That is the
right shape when the work is known, and the wrong one when a task arrives as a
sentence and the team that should do it has to be invented on the spot.

`agentos/agents/llm.py` is the other shape: **one agent class whose parameters
are its identity** — a role, a goal, a set of tools, a model class. Creating an
agent at runtime means constructing those four values, which are JSON, so a
runtime-invented agent still satisfies the Phase 1 rule that an agent is
re-creatable from its spec. It journals, recovers, and runs in a subprocess
like anything else. The planner is not a special class either; it is the same
`LLMAgent` with `may_spawn=True`.

```bash
python -m agentos.cli run examples/planner.py
```

That example hands the runtime one sentence — *"perform an experiment about
trees"* — and no workflow graph and no agent definitions. The planner invents a
Surveyor and an Analyst, grants each of them tools, waits for both, and
answers. It is fully offline: the model is a scripted mock, so what it
demonstrates is the runtime rather than a model's cleverness.

**Authority is the interesting part.** An agent may delegate only a subset of
what it holds:

```python
await ctx.spawn(LLMAgent(role="Surveyor", goal="...", tools=["filesystem"]),
                grant=["filesystem"])          # refused if the parent lacks it
```

The kernel checks that at spawn, which makes the capability set handed to the
root agent **the ceiling for the entire task tree** — however many layers of
agents a model invents, and whatever it decides to call them. Start the planner
with `["filesystem"]` and nothing underneath it can reach a shell. Grants live
on the process rather than the class (every dynamic agent would otherwise be
named `LLMAgent`), a narrower grant is never re-widened by the name matrix, and
authority dies with the process that held it.

That is what makes an unpredictable agent tree bounded: you cannot answer
"what could this touch?" by reading code that does not exist yet, so the kernel
answers it instead.

**The invented agents coordinate through the event bus, like everything else.**
Agents never call each other — but somebody has to choose event names, and no
programmer is present to do it. So the parent chooses: a spawn carries the
events that child may publish and the ones it will wait for.

```json
{"action": "spawn", "role": "Surveyor", "tools": ["filesystem"],
 "publishes": ["MeasurementsReady"], "subscribes": []}
{"action": "spawn", "role": "Analyst",  "tools": [],
 "publishes": ["AnalysisReady"], "subscribes": ["MeasurementsReady"]}
```

Running `examples/planner.py` shows what flows:

```
pid 2  Surveyor  publishes=MeasurementsReady  waits_for=-
pid 3  Analyst   publishes=AnalysisReady      waits_for=MeasurementsReady

MeasurementsReady  from pid 2 -> woke pid 3
AnalysisReady      from pid 3 -> woke pid 1
```

The Surveyor and the Analyst never name each other; the runtime does the
waking. No event name appears anywhere in the source — the planner invented
both, and the only thing it was told is to use the same one on each side.

**Why the kernel records the wiring.** Events match by exact string, so a
publisher and a waiter who disagree on a name do not fail — the waiter never
wakes, and it surfaces as a stall. Because the parent named both sides, the
kernel knows the vocabulary, which buys two things a model cannot get on its
own: publishing a name it was not wired for is **refused with a message it
can act on**, and waiting for a name that *provably* nobody will publish
**fails immediately** instead of hanging. A wait is only refused when every
live agent has a declared vocabulary and none of them names it — kernel
events like `AgentFinished` are always waitable, and one unwired agent is
enough to keep the wait open.

Note the difference from tool grants. Attenuation exists for **security**;
event wiring exists for **correctness**. Publishing an event harms nobody, so
declarations bound nothing — they only turn a silent hang into a loud error.
A hand-written agent, wired by no one, still decides its own events exactly
as `examples/pipeline.py` always has.

**Over HTTP**, that is the whole hosted story — a sentence and a tool list in,
a team out:

```bash
python -m agentos.cli daemon --task-tools filesystem,http
```

```bash
curl -X POST localhost:7070/task -d '{
  "goal": "perform an experiment about trees",
  "tools": ["filesystem"]
}'
# -> {"pid": 1, "granted": ["filesystem"], "poll": "/task/1"}

curl localhost:7070/task/1
# -> status, result, and every agent the planner invented
```

Authority is bounded twice. `--task-tools` is what the *operator* will ever
allow a submitted task to hold; the request asks for a subset of that; and
attenuation carries it down the tree. A request for `shell` on the daemon
above is refused at the door with a 400, not discovered later in an audit
log. Everything else arriving over the network is treated the same way: the
goal is length-bounded, tool names must resolve to real drivers, and step and
child limits are clamped rather than trusted.

`POST /agents` takes the same `grant`, for when you are submitting an agent
class you wrote rather than a sentence.

### Memory

Six kinds of memory behind four verbs, backend invisible to agents
(`examples/memory.py`):

```python
await ctx.memory.store("draft", data)                      # working: private
await ctx.memory.store("finding", fact, kind="shared")     # publishes MemoryUpdated
await ctx.memory.store("note", text, kind="semantic")      # + vector
hits = await ctx.memory.retrieve(kind="semantic", query="who schedules agents?")
```

`working`/`scratchpad` are private to a pid and freed when it exits. `shared`
is the only way agents pass state — through the kernel, with an access list
(`ctx.memory.share(key, with_agent=pid)`), never by touching each other.
`longterm` and `semantic` are keyed by agent *name*, so they survive restarts
(run `examples/memory.py` twice: the counter climbs). `episodic` is the
agent's own history, written by the kernel, read-only. The semantic embedding
is a deterministic stdlib placeholder — swap it for a real model without any
agent changing. Per-agent memory bytes show in the `MEM` column of `agent ps`.

### Model routing

Agents request a capability class, never a model name
(`examples/assistant.py`):

```python
reply = await ctx.request_model("fast", prompt="Summarize: ...")
reply["text"], reply["model"], reply["cost"]
```

Routing lives in `.agentos/models.json`: each class is a candidate list tried
in order — a candidate is skipped when unavailable (its API key is not set, or
the prompt exceeds its context window) and a candidate that fails at call time
falls through to the next. Config order is the default because a hand-written
order is the most honest expression of a preference, but the runtime will
choose on the p.7 criteria instead when asked:

```json
{"classes": {"reasoning": {"prefer": "cheapest", "candidates": [ ... ]}}}
```

`cheapest` ranks by projected cost for *this* prompt at the same rates the
ledger bills; `fastest` by declared latency; `best` by declared quality.
Candidates that cannot fit the prompt sort last rather than vanishing, so a
failure still names them. The seeded "fast" chain is Claude Haiku 4.5 → a
local Ollama endpoint → an offline mock that always answers, so the same agent
code lands on a frontier model, a local model, or the mock depending purely on
runtime config. Tokens and cost are recorded per agent (`COST` column in
`agent ps`/`top`), every call publishes `ModelFinished`, and failures are on
the record too. Providers: `anthropic` (official SDK when installed, stdlib
HTTP otherwise), `openai`-compatible (OpenAI, Ollama, vLLM, LM Studio),
`litellm` (optional), `mock`. The tests and most examples run fully offline.

### Crash recovery

You cannot snapshot a running coroutine — and you do not need to. Everything
an agent does crosses the syscall boundary as JSON, so the kernel journals
every syscall reply. **Every completed syscall is a checkpoint** — the `CKPT`
column in `agent ps`.

```bash
python -m agentos.cli run examples/crash.py    # 3 workers x 5 slow steps
kill -9 <os_pid>                               # mid-run, no cleanup, no mercy
python -m agentos.cli recover
```

An agent can also mark a durable point by name:

```python
n = await ctx.checkpoint("draft complete")   # p.9's kernel.checkpoint()
```

Recovery never needs it — a journaled syscall is a checkpoint whether or not
anyone asked — but it flushes the write-ahead log and moves the agent through
the `Checkpointing` state, which is what makes that p.3 state observable in
`agent ps` and `agent logs` rather than one the design implies and nobody can
ever see.

Recovery re-creates each agent from its spec and re-runs it; journaled
syscalls return their recorded replies instantly instead of re-executing — a
tool does not run twice, a model is not billed twice, a child is not spawned
twice, `MemoryUpdated` is not re-published — until the agent catches up to
where it died and goes live (`agent logs | grep recover` narrates it). The
crash log ends with every (worker, step) pair exactly once: a hard kill cost
the work since the last completed syscall and nothing more. The benchmark
above measures exactly this: **0 steps re-executed**.

The rest of the kernel state comes back the same way: finished children return
with their results so a waiting parent still resolves; a pending approval
re-attaches to the same durable approval row; events that were buffered but
never consumed are redelivered (consumption is part of the record). If a
replayed agent makes different syscalls than it made last time —
nondeterminism outside the boundary — the divergence is detected, logged, and
the agent simply goes live from there.

### The shared runtime daemon

The runtime is a process that outlives any application:

```bash
python -m agentos.cli daemon          # terminal 1: the runtime
python examples/app_research.py       # terminal 2: an application
python examples/app_support.py        # terminal 3: another one
python -m agentos.cli ps              # everyone's agents, one table, one cost ledger
```

Applications are thin clients (`agentos.RuntimeClient`): they submit an agent
as its *spec* — module, class, params, all JSON — and own nothing. No kernel,
no event loop; they can exit after submitting and the agent keeps running.
The daemon owns scheduling, permissions, memory, models, journaling, and
recovery (`agent daemon --recover` replays a crashed daemon's journals) for
every application at once. The control plane is HTTP + JSON, served stdlib
(`api/server.py`) — the routes are the API; FastAPI would be a drop-in
transport swap.

By default the daemon runs each agent as a **real OS process**
(`--isolation process`) with syscalls carried over a **loopback TCP socket**
(`--transport socket`) — both defaults, both swappable at the command line
(`--isolation task` for asyncio tasks, `--transport pipe` for stdio). See
"The one architectural decision" above for exactly what each mode means.
`agentos/runtime/child.py` reuses the *same* `Context` class agents have
always had — its queues just end at a socket or a pipe now, with `Syscall`
and `Reply` crossing as JSON lines. Not a line of `agents/` or `kernel/`
changed for either transport. Slots, pause-at-syscall, journal replay — all
of it works on subprocess agents unchanged, `agent kill` kills an actual
process, and `/health` reports which isolation and transport a running daemon
is using.

Honest scope: subprocesses give agents a separate address space (they
physically cannot reach the kernel or each other), but a malicious agent's
imports are not sandboxed — that would take OS-level confinement, which is out
of scope here. Likewise, an *in-process* agent that imports sqlite3 behind the
kernel's back is not physically stopped; the kernel-side capability check and
audit trail are what the permission system guarantees.

### Dashboard and example applications

The daemon serves a live dashboard at `http://127.0.0.1:7070/` with the p.8
panel list: running, waiting, and blocked agents, the live dependency graph,
the event timeline, memory, model usage, tool usage, latency, cost, and GPU
utilisation — polling the same JSON API everything else uses. One HTML file,
vanilla JS, no build step; the API is the interesting part and the page is a
window onto it.

GPU is reporting only, and says `none` on a machine without one. AgentOS does
not *schedule* GPU memory — p.4 lists GPU-aware scheduling as a future idea,
and claiming it would be worse than reporting honestly. Detection shells out
to `nvidia-smi`, whose absence is an answer rather than an error.

Three full applications exercise everything at once, offline:
`software_company.py` (events wake the team, code lands via the sandboxed
filesystem driver, and shipping blocks on a Release Manager's approval),
`research_assistant.py` (parallel searchers coordinate through shared and
semantic memory only), `customer_support.py` (tickets arrive as events, get
classified by model calls, and routed to specialists).

---

## Try it

No installs, no API keys — the kernel is demonstrated with agents that only
sleep, so scheduling is deterministic and a bug reproduces the same way twice.

```bash
python -m unittest discover tests -v          # 152 tests

python -m agentos.cli run examples/tree.py --slots 2      # processes + scheduling
python -m agentos.cli run examples/pipeline.py            # events + dependencies
python -m agentos.cli run examples/deadlock.py            # neither stall mode hangs
python -m agentos.cli run examples/deploy.py              # human approval
python -m agentos.cli run examples/finance.py             # tools + permissions
python -m agentos.cli run examples/memory.py              # six memory kinds
python -m agentos.cli run examples/assistant.py           # model routing
python -m agentos.cli run examples/crash.py               # kill -9 it, then:
python -m agentos.cli recover                             # nothing runs twice
python -m agentos.cli run examples/planner.py             # no graph, no agent
#   classes: a sentence goes in, the planner invents the team, and the
#   operator's tool grant is the ceiling for everything it creates

python -m agentos.cli daemon                              # the shared runtime
#   agents run as OS processes, syscalls over loopback TCP; opt out with
#   --isolation task or --transport pipe
python examples/app_research.py                           # app 1, another terminal
python examples/app_support.py                            # app 2, another terminal
# dashboard: http://127.0.0.1:7070/                       # live, while it runs

python -m agentos.cli run examples/software_company.py    # the full applications
python -m agentos.cli run examples/research_assistant.py
python -m agentos.cli run examples/customer_support.py

python benchmarks/bench.py                                # the numbers above
python benchmarks/compare.py                              # vs the other frameworks
```

Watch any run live from a second terminal:

```bash
python -m agentos.cli top          # live process table
python -m agentos.cli ps           # one-shot snapshot (the p.3 card per agent)
python -m agentos.cli wait 3       # block until pid 3 terminates; exits 0/1
python -m agentos.cli events -v    # who published what, and whom it woke
python -m agentos.cli logs         # every state transition
python -m agentos.cli kill 3       # kill a child; the parent survives
python -m agentos.cli pause 4      # suspends at its next syscall
python -m agentos.cli resume 4
python -m agentos.cli approvals    # pending human decisions
python -m agentos.cli approve 1 --as "Senior Engineer"
python -m agentos.cli tools        # drivers + the permission matrix
python -m agentos.cli grant Finance sql
python -m agentos.cli revoke Finance sql   # applies to a running system
```

---

## Layout

```
agentos/
  kernel/     states.py process.py scheduler.py messages.py store.py
              events.py depgraph.py permissions.py memory.py models.py kernel.py
              gpu.py               # utilisation reporting; None without a GPU
  drivers/    base.py              # timeout / rate limit / retry discipline, once
              filesystem.py shell.py python.py sql.py http.py browser.py
  runtime/    executor.py          # runs agents as asyncio tasks; owns Context
              subproc.py child.py  # ...or as real OS processes; same Context,
                                   #   syscalls over loopback TCP (default) or stdio
              daemon.py            # the shared runtime that outlives applications
  api/        server.py            # the daemon's HTTP control plane (stdlib)
              dashboard.py         # the live dashboard served at /
  agents/     base.py              # Agent, the direct-invocation guard, spec loader
              llm.py               # LLMAgent: params are the identity, so an
                                   #   agent can be invented at runtime
  client.py                        # RuntimeClient: the thin client applications use
  cli.py                           # agent ps / top / wait / events / logs / approvals /
                                   #   tools / kill / pause / resume / approve / grant /
                                   #   revoke / recover / daemon
examples/     tree.py              # a five-agent tree queuing for two slots
              pipeline.py          # the event pipeline + dependency graph
              deadlock.py          # both stall modes, neither hangs
              deploy.py            # blocks on a human; the approval survives restarts
              finance.py           # the permission matrix: sql granted, browser denied
              memory.py            # six memory kinds; longterm survives restarts
              assistant.py         # LLM calls; model choice is runtime config
              crash.py             # kill -9 mid-run; recover; nothing runs twice
              planner.py           # one sentence in; the team is invented on
                                   #   the spot, bounded by the operator's grant
              app_research.py      # thin client #1 — owns no runtime
              app_support.py       # thin client #2 — same daemon, same ps
              software_company.py  # the full applications: everything at once
              research_assistant.py customer_support.py
benchmarks/   bench.py             # recovery, approval latency, multi-app cost
              compare.py           # vs LangGraph / CrewAI / AutoGen / Temporal
              _temporal_defs.py    # workflows: Temporal re-imports these
              _autogen_defs.py     # handler types AutoGen resolves at import
tests/        test_kernel.py test_events.py test_approvals.py test_tools.py
              test_memory.py test_models.py test_recovery.py test_daemon.py
              test_spec_gaps.py    # the design-doc items an audit found missing
              test_dynamic_agents.py  # runtime-invented agents; the grant ceiling
              test_task_api.py     # the hosted path, and what it refuses
```
