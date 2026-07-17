# AgentOS

An operating system-inspired runtime for autonomous AI agents. Linux abstracts
hardware; AgentOS abstracts intelligence. Agents are processes, not objects: the
runtime owns their lifecycle, scheduling, dependencies, memory, tools, and
recovery.

Design doc: `AgentOS.pdf` (the phase roadmap is appended at the end of it).

## Status

| Phase | What it delivers | State |
|-------|------------------|-------|
| 1 | Process table, 9-state lifecycle, FIFO scheduler, spawn/kill/pause/resume/wait, CLI | **Done** |
| 2 | Event bus, dependency graph, priority + dependency-aware scheduling, deadlock detection | **Done** |
| 3 | Human approval as a kernel object | **Done** |
| 4 | Permissions and tool drivers | **Done** |
| 5 | Memory manager, then model routing (first LLM calls) | **Done** |
| 6 | Checkpoints and crash recovery | **Done** |
| 7 | Shared runtime daemon, OS-process agents | **Done** |
| 8 | Dashboard, examples, benchmark | **Done** |

All eight phases of the design doc are implemented. Zero dependencies, stdlib
only; everything below is verifiable offline.

## Verify it yourself

No installs, no API keys — the kernel is demonstrated with agents that only
sleep, so scheduling is deterministic and a bug reproduces the same way twice.

```bash
python -m unittest discover tests -v          # 83 tests

python -m agentos.cli run examples/tree.py --slots 2      # processes (p.8)
python -m agentos.cli run examples/pipeline.py            # events + deps (p.5)
python -m agentos.cli run examples/deadlock.py            # neither run hangs
python -m agentos.cli run examples/deploy.py              # human approval (p.5-6)
python -m agentos.cli run examples/finance.py             # tools + permissions (p.6-7)
python -m agentos.cli run examples/memory.py              # six memory kinds (p.6)
python -m agentos.cli run examples/assistant.py           # model routing (p.7)
python -m agentos.cli run examples/crash.py               # kill -9 it, then:
python -m agentos.cli recover                             # nothing runs twice (p.7-8)

python -m agentos.cli daemon                              # the shared runtime (p.8)
python examples/app_research.py                           # app 1, another terminal
python examples/app_support.py                            # app 2, another terminal
# dashboard: http://127.0.0.1:7070/                       # live, while it runs

python -m agentos.cli run examples/software_company.py    # the p.10 applications
python -m agentos.cli run examples/research_assistant.py
python -m agentos.cli run examples/customer_support.py

python benchmarks/bench.py                                # the numbers (p.11)
```

`--slots 2` means only two agents may hold an execution slot at once, so the
five-agent tree from the design doc is forced to queue. Watch it live from a
second terminal:

```bash
python -m agentos.cli top          # live process table
python -m agentos.cli ps           # one-shot snapshot
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

Scheduling policy is swappable at the command line:
`--policy fifo | priority | dependency`.

## Events and dependencies (Phase 2)

`examples/pipeline.py` is the p.5 scenario. `Research` publishes a fact and
stops — it never mentions `CodeAgent` or `DocumentationAgent`, and does not know
they exist:

```python
await ctx.publish("ResearchCompleted", topic=topic, findings=findings)
```

The runtime wakes whoever subscribed. Adding a fourth subscriber (`Reviewer`)
required editing no other agent. That isn't a style rule: `Context` gives an
agent no way to name another agent, and `Agent.run` is wrapped so that calling
another agent's `run()` directly raises `DirectInvocationError`.

Agents wait on a dependency *set*, not a sequence — and the scheduler wakes them
when the last one resolves:

```python
result = await ctx.wait_all(agents=[code, docs], events=["HumanApproved"], timer=5)
```

A wait that would close a cycle is refused when it is requested. A stall with no
cycle — everyone `Waiting`, nobody `Sleeping`, no timer pending — is detected and
reported. Neither one hangs.

## Human approval (Phase 3)

`examples/deploy.py` stops at:

```python
approval = await ctx.request_approval(role="Senior Engineer", reason="Production deployment")
```

`agent ps` shows `Blocked`, waiting on `Senior Engineer`. The human is a node in
the dependency graph — identical in kind to an agent, an event, or a timer — and

```bash
python -m agentos.cli approve 1 --as "Senior Engineer"
```

wakes the Deployer exactly where it stopped (approving `--as "Intern"` is
refused). An agent blocked on a human is not a deadlock; the runtime keeps
serving.

The approval is a durable kernel object, not a callback. Kill the runtime while
it is blocked, run it again, and the re-run agent re-attaches to the same
pending approval instead of asking twice — `agent approve` writes the grant to
the store, not to the process, so a human can even approve while nothing is
running and the next run sails through. Every grant also publishes a
`HumanApproved` event, so `wait_all(events=["HumanApproved"])` composes with it.

## Tools and permissions (Phase 4)

Agents never import tool libraries. They request a capability by name, and the
kernel dispatches to the driver that owns authentication, rate limiting,
retries, timeouts, and error handling:

```python
rows = await ctx.request_tool("sql", "query", query="SELECT SUM(amount) FROM invoices")
```

The kernel validates the capability against the permission matrix
(`.agentos/permissions.json`, agent name → capabilities, deny by default)
*before* dispatch — the application does not get a vote, and a denial is an
audit-log entry the agent can catch, not a stack trace. `examples/finance.py`
is the p.7 matrix: its SQL calls run, its browser call is refused. Revoke with
`agent revoke Finance sql` and the same code fails at its first query — the
file is re-read when it changes, so revocation applies to a *running* system.

Six drivers ship (`agent tools`): filesystem (sandboxed to a root, publishes
`FileCreated`), shell, python (fresh interpreter), sql, http, browser. A
running tool call is a dependency-graph node like any other — the agent shows
`Waiting on tool sql` in `agent ps`, completion publishes `ToolCompleted`, and
the woken agent re-queues for a slot like everyone else.

What this phase does **not** claim: an in-process agent that imports sqlite3
behind the kernel's back is not physically stopped yet. That isolation arrives
in Phase 7, when agents move into OS subprocesses; the kernel-side capability
check and audit trail are complete now.

## Memory (Phase 5)

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

## Model routing (Phase 5)

Agents request a capability class, never a model name (`examples/assistant.py`):

```python
reply = await ctx.request_model("fast", prompt="Summarize: ...")
reply["text"], reply["model"], reply["cost"]
```

Routing lives in `.agentos/models.json`: each class is a candidate list tried
in order — a candidate is skipped when unavailable (its API key is not set, or
the prompt exceeds its context window) and a candidate that fails at call time
falls through to the next. The seeded "fast" chain is Claude Haiku 4.5 → a
local Ollama endpoint → an offline mock that always answers, so the same agent
code lands on a frontier model, a local model, or the mock depending purely on
runtime config. Tokens and cost are recorded per agent (`COST` column in
`agent ps`/`top`), every call publishes `ModelFinished`, and failures are on
the record too. Providers: `anthropic` (official SDK when installed, stdlib
HTTP otherwise), `openai`-compatible (OpenAI, Ollama, vLLM, LM Studio),
`litellm` (optional), `mock`. Tests and every example before this phase remain
fully offline.

## Crash recovery (Phase 6)

You cannot snapshot a running coroutine — and you do not need to. Everything
an agent does crosses the syscall boundary as JSON (the Phase 1 rule), so the
kernel journals every syscall reply. **Every completed syscall is a
checkpoint** — the `CKPT` column in `agent ps`, and the p.3 card's
"Checkpoint: #31".

```bash
python -m agentos.cli run examples/crash.py    # 3 workers x 5 slow steps
kill -9 <os_pid>                               # mid-run, no cleanup, no mercy
python -m agentos.cli recover
```

Recovery re-creates each agent from its spec and re-runs it; journaled
syscalls return their recorded replies instantly instead of re-executing — a
tool does not run twice, a model is not billed twice, a child is not spawned
twice, `MemoryUpdated` is not re-published — until the agent catches up to
where it died and goes live (`agent logs | grep recover` narrates it). The
crash log ends with every (worker, step) pair exactly once: a hard kill cost
the work since the last completed syscall and nothing more.

The rest of the kernel state comes back the same way: finished children return
with their results so a waiting parent still resolves; a pending approval
re-attaches to the same durable approval row; events that were buffered but
never consumed are redelivered (consumption is part of the record). If a
replayed agent makes different syscalls than it made last time —
nondeterminism outside the boundary — the divergence is detected, logged, and
the agent simply goes live from there.

## The shared runtime daemon (Phase 7)

The runtime becomes a process that outlives any application:

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

And the executor swap: by default the daemon runs each agent as a **real OS
subprocess** (`--isolation process`, also `Kernel(isolation="process")`).
`agentos/runtime/child.py` reuses the *same* `Context` class agents have
always had — its queues just end at a pipe now, with `Syscall` and `Reply`
crossing as JSON lines. Not a line of `agents/` or `kernel/` changed, which
was the Phase 1 bet stated on day one: anything that survives `json.dumps`
survives a pipe. Slots, pause-at-syscall, journal replay — all of it works on
subprocess agents unchanged, and `agent kill` now kills an actual process.

Honest scope: subprocesses give agents a separate address space (they
physically cannot reach the kernel or each other), but a malicious agent's
imports are still not sandboxed — that would take OS-level confinement, which
is beyond this phase.

## Dashboard, applications, and the numbers (Phase 8)

The daemon serves a live dashboard at `/` — running/waiting/blocked agents,
the live dependency graph, the event timeline, memory, and cost, polling the
same JSON API everything else uses. One HTML file, vanilla JS, no build step;
the API is the interesting part and the page is a window onto it.

The three p.10 applications exercise everything at once, offline:
`software_company.py` (events wake the team, code lands via the sandboxed
filesystem driver, and shipping blocks on a Release Manager's approval),
`research_assistant.py` (parallel searchers coordinate through shared and
semantic memory only), `customer_support.py` (tickets arrive as events, get
classified by model calls, and routed to specialists).

`benchmarks/bench.py` measures the p.11 claims instead of asserting them —
representative numbers from this machine:

| claim | measurement |
|---|---|
| a hard kill costs nothing beyond the last syscall | 18-step workload, killed a third in: **0 steps re-executed**, recovery 0.8s |
| humans wake agents at scheduler speed | approve → finished: **~29ms median** (10ms tick) |
| one runtime accounts for every application | 3 apps × 5 agents × 2 calls: 22 agents/s, ledger **exact to the token** |

The workloads are framework-shaped (step pipelines, an approval gate,
N clients × M agents), so a LangGraph or CrewAI comparator can slot in as
another column by anyone with those installed; the doc's head-to-head remains
future work.

## The one architectural decision

Agents are **asyncio tasks behind a strict message-passing boundary**. An agent
never holds a reference to the kernel, the process table, or another agent — its
entire world is `Context` (`spawn`, `sleep`, `wait`, `log`), and every call
crosses a queue as a JSON-serializable `Syscall`, coming back as a `Reply`.

That constraint is enforced at runtime (`assert_serializable`), and it is what
makes Phase 7 cheap: anything that can survive `json.dumps` can survive a pipe,
so agents can move into real OS subprocesses without a line of agent code
changing.

## Layout

```
agentos/
  kernel/     states.py process.py scheduler.py messages.py store.py
              events.py depgraph.py permissions.py memory.py models.py kernel.py
  drivers/    base.py              # timeout / rate limit / retry discipline, once
              filesystem.py shell.py python.py sql.py http.py browser.py
  runtime/    executor.py          # runs agents as asyncio tasks; owns Context
              subproc.py child.py  # ...or as real OS processes; same Context
              daemon.py            # the shared runtime that outlives applications
  api/        server.py            # the daemon's HTTP control plane (stdlib)
              dashboard.py         # the live dashboard served at /
  agents/     base.py              # Agent, the direct-invocation guard, spec loader
  client.py                        # RuntimeClient: the thin client applications use
  cli.py                           # agent ps / top / events / logs / approvals / tools /
                                   #   kill / pause / resume / approve / grant / revoke /
                                   #   recover / daemon
examples/     tree.py              # the p.8 agent tree
              pipeline.py          # the p.5 event pipeline + dependency graph
              deadlock.py          # both stall modes, neither hangs
              deploy.py            # blocks on a human; the approval survives restarts
              finance.py           # the p.7 matrix: sql granted, browser denied
              memory.py            # six memory kinds; longterm survives restarts
              assistant.py         # first LLM call; model choice is runtime config
              crash.py             # kill -9 mid-run; recover; nothing runs twice
              app_research.py      # thin client #1 — owns no runtime
              app_support.py       # thin client #2 — same daemon, same ps
              software_company.py  # the p.10 applications: everything at once
              research_assistant.py customer_support.py
benchmarks/   bench.py             # recovery, approval latency, multi-app cost
tests/        test_kernel.py test_events.py test_approvals.py test_tools.py
              test_memory.py test_models.py test_recovery.py test_daemon.py
```

A woken agent goes back to `Ready` and re-queues for a slot rather than resuming
instantly. That is the difference between a scheduler and a callback, and there
is a test that fails if it regresses.
