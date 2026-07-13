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
| 3 | Human approval as a kernel object | Next |
| 4 | Permissions and tool drivers | |
| 5 | Memory manager, then model routing (first LLM calls) | |
| 6 | Checkpoints and crash recovery | |
| 7 | Shared runtime daemon | |
| 8 | Dashboard, examples, benchmark | |

## Verify it yourself

No installs, no API keys — the kernel is demonstrated with agents that only
sleep, so scheduling is deterministic and a bug reproduces the same way twice.

```bash
python -m unittest discover tests -v          # 28 tests

python -m agentos.cli run examples/tree.py --slots 2      # processes (p.8)
python -m agentos.cli run examples/pipeline.py            # events + deps (p.5)
python -m agentos.cli run examples/deadlock.py            # neither run hangs
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
              events.py depgraph.py kernel.py
  runtime/    executor.py          # runs agents; owns Context
  agents/     base.py              # Agent, and the direct-invocation guard
  cli.py                           # agent ps / top / events / logs / kill / pause / resume
examples/     tree.py              # the p.8 agent tree
              pipeline.py          # the p.5 event pipeline + dependency graph
              deadlock.py          # both stall modes, neither hangs
tests/        test_kernel.py test_events.py
```

A woken agent goes back to `Ready` and re-queues for a slot rather than resuming
instantly. That is the difference between a scheduler and a callback, and there
is a test that fails if it regresses.
