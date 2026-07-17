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
| 6 | Checkpoints and crash recovery | Next |
| 7 | Shared runtime daemon | |
| 8 | Dashboard, examples, benchmark | |

## Verify it yourself

No installs, no API keys — the kernel is demonstrated with agents that only
sleep, so scheduling is deterministic and a bug reproduces the same way twice.

```bash
python -m unittest discover tests -v          # 65 tests

python -m agentos.cli run examples/tree.py --slots 2      # processes (p.8)
python -m agentos.cli run examples/pipeline.py            # events + deps (p.5)
python -m agentos.cli run examples/deadlock.py            # neither run hangs
python -m agentos.cli run examples/deploy.py              # human approval (p.5-6)
python -m agentos.cli run examples/finance.py             # tools + permissions (p.6-7)
python -m agentos.cli run examples/memory.py              # six memory kinds (p.6)
python -m agentos.cli run examples/assistant.py           # model routing (p.7)
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
  runtime/    executor.py          # runs agents; owns Context (and ctx.memory)
  agents/     base.py              # Agent, and the direct-invocation guard
  cli.py                           # agent ps / top / events / logs / approvals / tools /
                                   #       kill / pause / resume / approve / grant / revoke
examples/     tree.py              # the p.8 agent tree
              pipeline.py          # the p.5 event pipeline + dependency graph
              deadlock.py          # both stall modes, neither hangs
              deploy.py            # blocks on a human; the approval survives restarts
              finance.py           # the p.7 matrix: sql granted, browser denied
              memory.py            # six memory kinds; longterm survives restarts
              assistant.py         # first LLM call; model choice is runtime config
tests/        test_kernel.py test_events.py test_approvals.py test_tools.py
              test_memory.py test_models.py
```

A woken agent goes back to `Ready` and re-queues for a slot rather than resuming
instantly. That is the difference between a scheduler and a callback, and there
is a test that fails if it regresses.
