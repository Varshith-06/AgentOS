"""The kernel loop.

One event loop, one process table, one scheduler. Agents run only when the
scheduler hands them a slot, and they re-enter the ready queue every time they
give one up — a woken agent does not resume instantly, it queues like everyone
else. That is the difference between a scheduler and a callback.

Phase 2 adds the two things that make the order emergent rather than written
down: agents publish events instead of calling each other, and they wait on a
dependency graph instead of a sequence. Nothing in here knows what a workflow
is.

Phase 3 makes a human a dependency like any other: request_approval blocks the
process and registers the named role as a node in the dependency graph. The
grant lives in the store, not in memory, so a pending approval survives a
runtime restart — the difference between a kernel object and a callback.

Phase 4 does the same for tools: an agent requests a capability by name, the
kernel validates it against the permission matrix before dispatch, and the
running call is a dependency-graph node the scheduler waits on like any other.
Agents never import a tool library; drivers own that.

Phase 5 adds what agents remember and what they think with. Memory is a kernel
service (six kinds behind store/retrieve/share/delete, backend invisible), and
models are routed: an agent asks for a capability class ("Need: fast"), never
a model name, and the kernel picks by availability, records tokens and cost
per agent, and publishes ModelFinished.

Phase 6 makes a hard kill survivable. Every syscall reply is journaled — the
Phase 1 rule that everything crossing the boundary must survive json.dumps is
exactly what makes the journal possible. On recovery the kernel re-creates
each agent from its spec and re-runs it; journaled syscalls return their
recorded replies instantly instead of re-executing (a tool does not run
twice, a model is not billed twice, a child is not spawned twice), so the
agent fast-forwards to where it died and goes live from there. A crash costs
the work since the last completed syscall and nothing more.

Phase 7 cashes in the boundary: agents can run as real OS subprocesses
(isolation="process"), with Syscall and Reply crossing an actual pipe instead
of an asyncio queue — the kernel cannot tell the difference. In daemon mode
the runtime outlives any application; thin clients submit agents as specs
over HTTP and one process table shows everyone's work.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import Any

from ..agents.base import Agent, agent_from_spec, spec_of
from ..drivers import REGISTRY, ToolError
from ..runtime.executor import Executor
from ..runtime.subproc import ProcessExecutor, SocketExecutor
from . import depgraph as dg
from . import gpu
from .depgraph import DependencyGraph
from .events import KERNEL_EVENTS, Event, EventBus, InvalidEvent
from .memory import MemoryManager
from .messages import NotSerializable, Reply, Syscall, assert_serializable
from .models import ModelError, ModelManager
from .permissions import PermissionDenied, Permissions
from .process import AgentProcess, ProcessTable
from .scheduler import SchedulerView, get_policy
from .states import TERMINAL, AgentState
from .store import Store

#: Syscalls that keep the agent's slot (it never stops running).
NONBLOCKING = {"spawn", "log", "publish", "subscribe", "memory", "checkpoint"}

#: States from which the system can still make progress on its own.
LIVE = frozenset(
    {
        AgentState.RUNNING,
        AgentState.READY,
        AgentState.SLEEPING,
        AgentState.SUSPENDED,  # a human can resume it
        AgentState.BLOCKED,  # a human can approve it
        AgentState.CHECKPOINTING,
    }
)


class Kernel:
    def __init__(
        self,
        policy: str = "fifo",
        slots: int = 4,
        store: Store | None = None,
        tick: float = 0.05,
        permissions: Any = None,
        tools: dict[str, dict[str, Any]] | None = None,
        models: Any = None,
        recover: bool = False,
        isolation: str = "task",
        transport: str = "socket",
        retries: int = 0,
        daemon: bool = False,
    ) -> None:
        self.table = ProcessTable()
        self.policy = get_policy(policy)
        self.slots = slots
        self.tick = tick
        #: Default restart budget for an agent that raises (p.4). An agent
        #: may override it with its own `retries` attribute.
        self.retries = retries
        self.store = store if store is not None else Store()

        # The p.7 permission matrix. None means: watch the standard file next
        # to the runtime state, so grants and revocations are config edits.
        self.perms = Permissions.of(permissions, self.store.dir / "permissions.json")
        self.tools_config = tools or {}
        self._drivers: dict[str, Any] = {}
        self._io_tasks: set[asyncio.Task] = set()
        self._io_calls = 0  # running tool/model calls: pending I/O is not a deadlock

        # Phase 5: memory as a kernel service, model choice as runtime config.
        self.memory = MemoryManager(self.store)
        self.models = ModelManager.of(
            models,
            self.store.dir / "models.json",
            log=lambda message: self._log(None, "model", message),
        )

        self.mailbox: asyncio.Queue[Syscall] = asyncio.Queue()
        #: Syscalls the idle wait pulled off the mailbox early; drained first.
        self._early: deque[Syscall] = deque()
        self._last_poll = 0.0  # when the tick-rate polling work last ran
        #: Set to cut an idle short. Created on the loop, so not in __init__.
        self._wakeup: asyncio.Event | None = None
        self.ready: deque[AgentProcess] = deque()
        self.running: set[int] = set()
        self.agents: dict[int, Agent] = {}

        self.bus = EventBus()
        self.deps = DependencyGraph()

        # Phase 7: agents as asyncio tasks or as real OS subprocesses. The
        # kernel cannot tell the difference — that is the message boundary.
        # With process isolation the syscall transport is itself swappable:
        # a loopback TCP socket (default) or stdio pipes.
        if isolation not in ("task", "process"):
            raise ValueError(f"isolation must be 'task' or 'process', not {isolation!r}")
        if transport not in ("socket", "pipe"):
            raise ValueError(f"transport must be 'socket' or 'pipe', not {transport!r}")
        self.isolation = isolation
        self.transport = transport if isolation == "process" else None
        if isolation == "process":
            executor_cls = SocketExecutor if transport == "socket" else ProcessExecutor
        else:
            executor_cls = Executor
        self.executor = executor_cls(self.mailbox, self._on_finish, self._on_fail)
        #: Daemon mode: the runtime outlives its work. Never exit on quiescence,
        #: and never declare a stall a deadlock — new work can always arrive.
        self.daemon_mode = daemon
        self.table.on_transition = self._publish_row
        self._shutdown = False
        self._timers = 0  # outstanding timer dependencies

        # Phase 6: the journal is the checkpoint. On a fresh boot it is
        # cleared; on recovery it is the script the agents replay from.
        self._recovered = recover
        if recover:
            self.store.resume_runtime(self.policy.name, slots)
            self._journals = self.store.load_journals()
            self._restore()
        else:
            self.store.register_runtime(self.policy.name, slots)
            self._journals = {}

    # -- crash recovery (Phase 6, p.7-8) -----------------------------------
    def _restore(self) -> None:
        """Rebuild the world from the store after a hard kill.

        Terminal processes come back with their results, so anything that was
        waiting on them still resolves. Live processes come back READY with a
        fresh task; their journal replays them forward to where they died.
        """
        for row in sorted(self.store.processes(), key=lambda r: r["pid"]):
            proc = self.table.restore(
                pid=row["pid"],
                name=row["name"],
                parent=row["parent"],
                spec=row.get("spec") or {},
                priority=row.get("priority", "Normal"),
                state=AgentState(row["status"]),
                started_at=row["started_at"],
            )
            proc.result = row.get("result")
            proc.exit_reason = row.get("exit_reason")
            proc.ended_at = row.get("ended_at")
            proc.checkpoint = len(self._journals.get(proc.pid, []))
            if not proc.alive:
                continue
            # Alive at the crash: re-create from spec, replay from journal.
            proc.state = AgentState.READY
            proc.waiting_on = None
            self.agents[proc.pid] = self._create_agent(proc.spec)
            self.ready.append(proc)
            self._publish_row(proc)
            self._log(
                proc.pid,
                "recover",
                f"{proc.name} restored; replaying {proc.checkpoint} journaled syscall(s)",
            )

        # The event bus continues, it does not start over: history and the
        # sequence counter pick up where the dead runtime left them.
        for row in self.store.events():
            self.bus.history.append(
                Event(row["type"], row["payload"], row["source_pid"], row["seq"])
            )
            self.bus._seq = max(self.bus._seq, row["seq"])

    def _create_agent(self, spec: dict[str, Any]) -> Agent:
        return agent_from_spec(spec)

    def _journal(self, proc: AgentProcess, req_id: int, op: str, value: Any,
                 error: str | None) -> None:
        """A delivered reply is a checkpoint: everything up to here is safe."""
        self.store.append_journal(proc.pid, req_id, op, value, error)
        proc.checkpoint += 1
        self.store.publish(proc.row())

    def _replay_entry(self, proc: AgentProcess, call: Syscall) -> dict[str, Any] | None:
        """The journaled reply for this syscall, if we are replaying one."""
        entries = self._journals.get(proc.pid)
        if not entries:
            return None
        entry = entries[0]
        if entry["req_id"] != call.req_id or entry["op"] != call.op:
            # The agent did not make the same syscalls it made last time —
            # nondeterminism outside the boundary. Stop replaying, go live.
            self._log(
                proc.pid,
                "recover",
                f"replay diverged at #{call.req_id}: journal has "
                f"{entry['op']!r}#{entry['req_id']}, agent asked {call.op!r}; going live",
            )
            del self._journals[proc.pid]
            return None
        entries.pop(0)
        if not entries:
            del self._journals[proc.pid]
            self._log(proc.pid, "recover", f"{proc.name} caught up; live from here")
        return entry

    # -- public API (AgentOS.pdf p.9) ------------------------------------
    def spawn(self, agent: Agent, parent: int | None = None) -> int:
        proc = self.table.create(
            name=agent.name,
            spec=spec_of(agent),
            parent=parent,
            priority=getattr(agent, "priority", "Normal"),
        )
        self.agents[proc.pid] = agent
        proc.permissions = sorted(self.perms.capabilities(proc.name, proc.pid))
        self.ready.append(proc)
        self._publish_row(proc)
        self._log(
            proc.pid,
            "spawn",
            f"{proc.name} spawned" + (f" by pid {parent}" if parent else ""),
        )
        return proc.pid

    def kill(self, pid: int, *, reason: str = "killed") -> None:
        """Terminate a process and its descendants. Ancestors are untouched."""
        proc = self.table.get(pid)
        for child in list(proc.children):
            child_proc = self.table.get(child)
            if child_proc.alive:
                self.kill(child, reason=f"parent pid {pid} {reason}")
        if not proc.alive:
            return
        self._cancel_timer(proc)
        self.deps.cancel(pid)
        if proc.task is not None and not proc.task.done():
            proc.exit_reason = reason  # _on_fail preserves a reason already set
            proc.task.cancel()  # surfaces as FAILED(killed) via the executor
        else:
            # Never started: it was sitting in the ready queue.
            self._discard_ready(proc)
            proc.exit_reason = reason
            self.table.transition(proc, AgentState.FAILED)
            self._log(pid, "kill", f"{proc.name} killed before execution")
            self._announce_exit(proc)
        self.running.discard(pid)

    def pause(self, pid: int) -> None:
        proc = self.table.get(pid)
        if not proc.alive or proc.state is AgentState.SUSPENDED:
            return
        proc.pause_requested = True
        if proc.state is not AgentState.RUNNING:
            self._discard_ready(proc)
            # The timer keeps running: if it fires while suspended, the wake is
            # stashed on the process (see _requeue) and delivered at resume.
            self.table.transition(proc, AgentState.SUSPENDED, waiting_on="resume")
            proc.pause_requested = False
            self._log(pid, "pause", f"{proc.name} suspended")

    def resume(self, pid: int) -> None:
        proc = self.table.get(pid)
        proc.pause_requested = False
        if proc.state is not AgentState.SUSPENDED:
            return
        if (
            proc.task is not None
            and proc.pending is None
            and (self.deps.is_waiting(pid) or proc.timer is not None)
        ):
            # Suspended mid-wait, and the wait has not resolved yet. Waking it
            # now would hand it nothing. It stays suspended; the resolution is
            # stashed when it arrives, and a later resume delivers it.
            self._log(
                pid,
                "resume",
                f"{proc.name} is still mid-wait; resume again once it resolves",
            )
            return
        self.table.transition(proc, AgentState.READY)
        self.ready.append(proc)
        self._log(pid, "resume", f"{proc.name} resumed")

    def approve(self, pid: int, role: str) -> None:
        """Satisfy a pending approval (p.6). Refused unless `role` matches.

        The CLI writes grants to the store directly (so a human can approve a
        runtime that is not running); this is the in-process equivalent.
        """
        self.store.approve(pid, role)
        self._drain_approvals()

    def publish(self, event_type: str, source_pid: int | None = None, **payload: Any):
        """Announce an event. The bus decides who hears it (p.5)."""
        # Who is mid-wait on this event type? They receive THIS publish through
        # dependency resolution, so the copy the bus is about to buffer for
        # them must be consumed too — one publish, one delivery, never two.
        waiting_pids = set(self.deps.dependents(dg.key(dg.EVENT, event_type)))

        event = self.bus.publish(event_type, payload, source_pid)
        subscribers = self.bus.subscribers(event_type)
        self.store.append_event(
            event.seq, event_type, source_pid, payload, subscribers
        )
        self._log(
            source_pid,
            "event",
            f"{event_type} published"
            + (f" -> woke {len(subscribers)} subscriber(s)" if subscribers else " (no subscribers)"),
        )
        for pid in waiting_pids:
            consumed = self.bus.consume(pid, event_type)
            if consumed is not None:
                self.store.record_consumption(pid, consumed.seq)
        # Anyone whose dependency this satisfies becomes runnable.
        for w in self.deps.resolve(dg.key(dg.EVENT, event_type), payload):
            self._wake_waiter(w)
        return event

    async def run(self) -> None:
        """Run until every process is terminal — or until nothing can progress.

        The loop is event-driven with a tick as its *ceiling*, not its period:
        a syscall wakes it immediately, and `tick` only bounds how long it may
        doze when nothing is happening. That distinction is worth more than it
        looks — `asyncio.sleep()` cannot resolve below the platform timer
        quantum (~15.6ms on Windows), so a loop that slept the tick every pass
        paid that quantum on every syscall no matter how small the tick was.
        Polling work — control commands, approvals, the permission file,
        deadlock detection — stays on the tick, where it belongs.
        """
        while not self._shutdown:
            now = time.time()
            polled = now - self._last_poll >= self.tick
            if polled:
                # Everything that reads the outside world — the permission
                # file, the command and approval tables, the heartbeat — is
                # rate-limited to the tick. These are the expensive ones.
                self._last_poll = now
                self.perms.refresh()  # a revocation applies to a running system
                self._drain_commands()
                self._drain_approvals()
                self.store.heartbeat()

            await self._drain_mailbox()
            self._admit()

            # Cheap and in-memory, so it runs every pass: an agent that just
            # finished should not wait out a tick for the runtime to notice.
            if self._quiescent() and not self.daemon_mode:
                break
            if polled:
                self._detect_deadlock()
            await self._idle(self.tick)
        for task in list(self._io_tasks):  # no owners remain for these
            task.cancel()
        if self._io_tasks:
            await asyncio.gather(*self._io_tasks, return_exceptions=True)
        close = getattr(self.executor, "aclose", None)
        if close is not None:  # the socket transport's listener
            await close()

    async def run_until_done(self, agent: Agent) -> Any:
        pid = self.spawn(agent)
        await self.run()
        return self.table.get(pid).result

    def submit_spec(self, spec: dict[str, Any], grant: list[str] | None = None) -> int:
        """Spawn from a serialized spec — how a thin client hands the daemon
        an agent it has never imported (p.8).

        `grant` pins this process's capabilities explicitly, which is how the
        operator sets the ceiling for a task submitted from outside: whatever
        the agent goes on to create, no descendant can exceed this set.
        """
        pid = self.spawn(self._create_agent(spec))
        if grant is not None:
            self.perms.assign(pid, set(grant))
            proc = self.table.get(pid)
            proc.permissions = sorted(grant)
            self._publish_row(proc)
            self._log(
                None, "grant",
                f"pid {pid} admitted with {', '.join(sorted(grant)) or 'nothing'}",
            )
        return pid

    def snapshot(self) -> dict[str, Any]:
        """One consistent view of the scheduler for the dashboard (p.8):
        who is running, who waits on whom, and what the policy is doing."""
        return {
            "policy": self.policy.name,
            "slots": self.slots,
            "isolation": self.isolation,
            "transport": self.transport,
            "gpu": gpu.summary(),  # None on a machine without one
            "running": sorted(self.running),
            "ready": [p.pid for p in self.ready],
            "processes": [p.row() for p in self.table.all()],
            "deps": [
                {"pid": pid, "waits_on": sorted(w.remaining)}
                for pid, w in self.deps.waiting.items()
            ],
        }

    # -- scheduling ------------------------------------------------------
    def _view(self) -> SchedulerView:
        return SchedulerView(dependents=self.deps.agent_dependents())

    def _admit(self) -> None:
        """Hand execution slots to READY processes, per the scheduling policy."""
        view = self._view()
        while self.ready and len(self.running) < self.slots:
            proc = self.policy.pick(self.ready, view)
            if not proc.alive or proc.state is AgentState.SUSPENDED:
                continue
            if proc.pause_requested:
                self.table.transition(proc, AgentState.SUSPENDED, waiting_on="resume")
                proc.pause_requested = False
                self._log(proc.pid, "pause", f"{proc.name} suspended")
                continue

            self.table.transition(proc, AgentState.RUNNING)
            self.running.add(proc.pid)
            if proc.task is None:
                self.executor.start(proc, self.agents[proc.pid])
            else:
                # Resuming: the reply it has been owed since it gave up its slot.
                reply, proc.pending = proc.pending, None
                proc.inbox.put_nowait(reply)

    def _yield_slot(
        self, proc: AgentProcess, state: AgentState, waiting_on: str
    ) -> None:
        self.running.discard(proc.pid)
        self.table.transition(proc, state, waiting_on=waiting_on)

    def _requeue(self, proc: AgentProcess, reply: Reply) -> None:
        """Owe `proc` a reply and put it back in line for a slot.

        The reply is journaled here — the moment it is committed to the
        process — not at delivery. If we die between the two, recovery replays
        it, which is exactly what "owed" means.
        """
        if not proc.alive:
            return
        self._journal(proc, reply.req_id, proc.current_op or "?", reply.value, reply.error)
        proc.pending = reply
        self._nudge()  # something became runnable: do not doze until the tick
        if proc.state is AgentState.SUSPENDED:
            return  # stashed; delivered when a human resumes it
        self.table.transition(proc, AgentState.READY, waiting_on=None)
        self.ready.append(proc)

    def _wake_waiter(self, w: dg.Waiting) -> None:
        """A dependency set is fully satisfied: the scheduler wakes the waiter."""
        proc = self.table.get(w.pid)
        self._requeue(proc, Reply(req_id=w.req_id, value=_shape(w.results)))

    # -- syscalls --------------------------------------------------------
    def _nudge(self) -> None:
        """Cut the current idle short: there is work the loop should see now."""
        if self._wakeup is not None:
            self._wakeup.set()

    async def _idle(self, timeout: float) -> None:
        """Doze until a syscall arrives, someone nudges, or `timeout` elapses.

        The early wake is the point: a pending syscall or a freshly-runnable
        agent is handled in microseconds instead of waiting out a timer
        quantum (~15.6ms on Windows). Anything pulled off the mailbox is
        stashed for _drain_mailbox rather than handled here, so the loop keeps
        exactly one place where syscalls are dispatched. Cancelling a pending
        `Queue.get()` never consumes an item, and _drain_mailbox runs on the
        very next line, so nothing can be stranded in the queue.
        """
        if self._wakeup is None:
            self._wakeup = asyncio.Event()
        getter = asyncio.ensure_future(self.mailbox.get())
        waker = asyncio.ensure_future(self._wakeup.wait())
        try:
            done, pending = await asyncio.wait(
                {getter, waker}, timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if getter in done and not getter.cancelled():
                self._early.append(getter.result())
        finally:
            for task in (getter, waker):
                if not task.done():
                    task.cancel()
            self._wakeup.clear()

    async def _drain_mailbox(self) -> None:
        while self._early or not self.mailbox.empty():
            call = self._early.popleft() if self._early else self.mailbox.get_nowait()
            proc = self.table.get(call.pid)
            try:
                self._handle(proc, call)
            except Exception as exc:  # kernel refuses; the agent sees the error
                if proc.state is AgentState.WAITING:  # it already gave up its slot
                    self._requeue(proc, Reply(req_id=call.req_id, error=str(exc)))
                else:
                    self._journal(proc, call.req_id, call.op, None, str(exc))
                    proc.inbox.put_nowait(Reply(req_id=call.req_id, error=str(exc)))

    def _handle(self, proc: AgentProcess, call: Syscall) -> None:
        entry = self._replay_entry(proc, call)
        if entry is not None:
            # Replaying: the world already changed once; do not change it
            # again. Subscribe is the exception — it rebuilds kernel state
            # that died with the old runtime, so it re-executes.
            if call.op == "subscribe":
                self._sys_subscribe(proc, **call.args)
            proc.inbox.put_nowait(
                Reply(req_id=call.req_id, value=entry["value"], error=entry["error"])
            )
            return
        if call.op in NONBLOCKING:
            value = getattr(self, f"_sys_{call.op}")(proc, **call.args)
            self._journal(proc, call.req_id, call.op, value, None)
            proc.inbox.put_nowait(Reply(req_id=call.req_id, value=value))
            return
        proc.current_op = call.op
        getattr(self, f"_sys_{call.op}")(proc, call, **call.args)

    def _sys_spawn(
        self,
        proc: AgentProcess,
        spec: dict[str, Any],
        grant: list[str] | None = None,
        publishes: list[str] | None = None,
        subscribes: list[str] | None = None,
    ) -> int:
        """Create a child: delegate capabilities to it, and wire its events.

        Two different rules, for two different problems.

        `grant` is *security*. Attenuation applies: a parent may hand a child
        any part of what it holds and nothing else, so a planner spawned with
        {http, sql} cannot produce a descendant that reaches the shell,
        however many layers of agents it invents on the way.

        `publishes`/`subscribes` are *correctness*. Events match by exact
        string, so a publisher and a waiter who disagree on a name do not
        error — the waiter simply never wakes. Letting the parent name both
        sides makes them come from one source, and recording what it named
        means a child that publishes something else is refused with a message
        it can act on, instead of hanging. Nothing here is a security
        boundary: publishing an event harms nobody.
        """
        child = self._create_agent(spec)
        if grant is None and publishes is None and subscribes is None:
            return self.spawn(child, parent=proc.pid)

        if grant is not None:
            mine = self.perms.capabilities(proc.name, proc.pid)
            wanted = set(grant)
            if "*" not in mine and not wanted <= mine:
                over = ", ".join(sorted(wanted - mine))
                raise PermissionDenied(
                    f"{proc.name} (pid {proc.pid}) cannot grant {over}: "
                    f"it holds only {', '.join(sorted(mine)) or 'nothing'}"
                )
        for names, what in ((publishes, "publishes"), (subscribes, "subscribes")):
            if names is not None and (
                not isinstance(names, list)
                or any(not isinstance(n, str) or not n.strip() for n in names)
            ):
                raise InvalidEvent(f"{what} must be a list of event type names")

        pid = self.spawn(child, parent=proc.pid)
        kid = self.table.get(pid)
        if grant is not None:
            self.perms.assign(pid, set(grant))
            kid.permissions = sorted(set(grant))
            self._log(
                proc.pid, "grant",
                f"{proc.name} granted pid {pid} "
                f"{', '.join(sorted(set(grant))) or 'nothing'}",
            )
        if publishes is not None:
            kid.publishes = list(dict.fromkeys(publishes))
        if subscribes is not None:
            kid.subscribes = list(dict.fromkeys(subscribes))
            # Subscribe on the child's behalf, now: an event fired before it
            # gets a slot must still be waiting in its inbox when it asks.
            for event_type in kid.subscribes:
                self.bus.subscribe(pid, event_type)
        if publishes or subscribes:
            self._log(
                proc.pid, "wire",
                f"{proc.name} wired pid {pid}: "
                f"publishes {', '.join(kid.publishes or []) or 'nothing'}; "
                f"waits for {', '.join(kid.subscribes or []) or 'nothing'}",
            )
        self._publish_row(kid)
        return pid

    def _sys_log(self, proc: AgentProcess, message: str) -> None:
        self._log(proc.pid, "agent", message)
        return None

    def _sys_checkpoint(self, proc: AgentProcess, label: str | None = None) -> int:
        """kernel.checkpoint() from p.9 — the explicit form of what every
        syscall already does implicitly.

        Recovery does not need this call: a journaled syscall is a checkpoint
        whether or not anyone asked. What it adds is the *observable* one —
        the agent passes through CHECKPOINTING, so the p.3 state appears in
        `agent ps` and `agent logs` instead of being a state the design
        implies but nobody can ever see.
        """
        was = proc.state
        if was is AgentState.RUNNING:
            self.table.transition(proc, AgentState.CHECKPOINTING, waiting_on="journal")
        self.store.flush()
        self._log(
            proc.pid, "checkpoint",
            f"{proc.name} checkpoint #{proc.checkpoint + 1}"
            + (f" ({label})" if label else ""),
        )
        if was is AgentState.RUNNING:
            self.table.transition(proc, AgentState.RUNNING, waiting_on=None)
        return proc.checkpoint + 1

    def _sys_publish(
        self, proc: AgentProcess, event_type: str, payload: dict[str, Any]
    ) -> None:
        if proc.publishes is not None and event_type not in proc.publishes:
            # The parent wired this agent for a specific vocabulary. Publishing
            # outside it is almost always a name the model invented on the
            # spot, which nobody is waiting for — so say so now, while there
            # is still something to correct.
            allowed = ", ".join(proc.publishes) or "nothing"
            raise InvalidEvent(
                f"{proc.name} was not wired to publish {event_type!r}; "
                f"it may publish: {allowed}"
            )
        self.publish(event_type, source_pid=proc.pid, **payload)
        return None

    def _unpublishable(self, event_type: str) -> bool:
        """Can we *prove* nobody will ever publish this event type?

        Only then is refusing a wait safe. The kernel publishes its own event
        types whenever it likes, and any live agent without a declared
        vocabulary may publish anything — so a wait is refused only when
        every live agent has a contract and none of them names this.
        """
        if event_type in KERNEL_EVENTS:
            return False
        for other in self.table.all():
            if not other.alive:
                continue
            if other.publishes is None:  # unrestricted: it might
                return False
            if event_type in other.publishes:
                return False
        return True

    def _sys_subscribe(self, proc: AgentProcess, event_types: list[str]) -> None:
        for event_type in event_types:
            self.bus.subscribe(proc.pid, event_type)
            self._log(proc.pid, "sub", f"{proc.name} subscribed to {event_type}")
            if self._recovered:
                self._backfill(proc.pid, event_type)
        return None

    def _backfill(self, pid: int, event_type: str) -> None:
        """Redeliver pre-crash events this subscriber was owed but never took.

        An event's subscriber list was recorded when it was published, so only
        agents that were genuinely subscribed back then qualify — and anything
        already consumed (per the consumptions table) stays consumed.
        """
        consumed = self.store.consumptions()
        queue = self.bus.inboxes[pid][event_type]
        buffered = {e.seq for e in queue}
        for row in self.store.events():
            if row["type"] != event_type or pid not in row["subscribers"]:
                continue
            if (pid, row["seq"]) in consumed or row["seq"] in buffered:
                continue
            queue.append(Event(row["type"], row["payload"], row["source_pid"], row["seq"]))
            self._log(pid, "recover", f"re-buffered pre-crash event {row['type']}")

    def _sys_sleep(self, proc: AgentProcess, call: Syscall, seconds: float) -> None:
        self._yield_slot(proc, AgentState.SLEEPING, f"timer {seconds}s")
        loop = asyncio.get_running_loop()
        self._timers += 1
        proc.timer = loop.call_later(
            seconds, self._on_timer, proc, Reply(req_id=call.req_id)
        )

    def _on_timer(self, proc: AgentProcess, reply: Reply) -> None:
        self._timers -= 1
        proc.timer = None
        self.publish("TimerExpired", source_pid=proc.pid)
        self._requeue(proc, reply)

    def _sys_wait_all(
        self,
        proc: AgentProcess,
        call: Syscall,
        agents: list[int],
        events: list[str],
        timer: float | None,
    ) -> None:
        """Declare a dependency set. The scheduler wakes us when it is complete."""
        if not agents and not events and timer is None:
            raise ValueError("wait_all() needs at least one dependency")

        targets = set(agents)
        if proc.pid in targets:
            raise dg.Deadlock(f"pid {proc.pid} cannot wait on itself")
        cycle = self.deps.cycle_from(proc.pid, targets)
        if cycle is not None:
            trail = " -> ".join(f"pid {p}" for p in cycle)
            self._log(proc.pid, "deadlock", f"cycle refused: {trail}")
            raise dg.Deadlock(
                f"waiting here would deadlock: {trail}. "
                "Break the cycle with an event instead of a direct wait."
            )

        deps: set[str] = set()
        resolved: dict[str, Any] = {}

        for pid in targets:
            target = self.table.get(pid)
            if target.alive:
                deps.add(dg.key(dg.AGENT, pid))
            else:
                resolved[dg.key(dg.AGENT, pid)] = target.result

        for event_type in events:
            self.bus.subscribe(proc.pid, event_type)  # idempotent
            buffered = self.bus.consume(proc.pid, event_type)
            if buffered is not None:  # it already fired while we were busy
                self.store.record_consumption(proc.pid, buffered.seq)
                resolved[dg.key(dg.EVENT, event_type)] = buffered.payload
                continue
            if timer is None and self._unpublishable(event_type):
                # Nobody alive is wired to publish this and the kernel never
                # will, so this wait can only end in the deadlock detector.
                # Failing here names the actual mistake — usually a
                # misremembered event name — while the waiter can still act.
                self._log(
                    proc.pid, "deadlock",
                    f"refused wait on {event_type!r}: nobody publishes it",
                )
                raise dg.Deadlock(
                    f"no live agent is wired to publish {event_type!r}, so "
                    "waiting for it would hang. Check the event name against "
                    "what you wired, or wait for one of your agents instead."
                )
            deps.add(dg.key(dg.EVENT, event_type))

        timer_key = dg.key(dg.TIMER, call.req_id)
        if timer is not None:
            deps.add(timer_key)

        self._yield_slot(proc, AgentState.WAITING, _describe(deps, self.table))

        w = self.deps.add(proc.pid, call.req_id, deps)
        w.results.update(resolved)

        if timer is not None:
            loop = asyncio.get_running_loop()
            self._timers += 1
            proc.timer = loop.call_later(timer, self._on_dep_timer, proc, timer_key)

        if w.satisfied:  # everything was already done — still costs a queue trip
            self.deps.waiting.pop(proc.pid, None)
            self._wake_waiter(w)

    def _sys_request_tool(
        self,
        proc: AgentProcess,
        call: Syscall,
        capability: str,
        op: str,
        params: dict[str, Any],
    ) -> None:
        """Dispatch a tool call through its driver (p.6-7).

        The kernel validates the capability before dispatch — the application
        does not get a vote — and the running call becomes a dependency-graph
        node, so the agent waits for it the way it waits for anything else.
        """
        if not isinstance(capability, str) or not capability.strip():
            raise ValueError("request_tool() needs a capability name")
        if not self.perms.allowed(proc.name, capability, proc.pid):
            self._log(
                proc.pid,
                "denied",
                f"{proc.name} requested {capability!r}: permission denied",
            )
            # Point at the right lever. An agent whose authority was delegated
            # cannot be fixed by editing the matrix — its parent decided this,
            # and widening the file would not (and must not) change it.
            if proc.pid in self.perms.pid_grants:
                held = ", ".join(sorted(self.perms.pid_grants[proc.pid])) or "nothing"
                remedy = (
                    f"it was delegated only: {held}. Whoever spawned it must "
                    f"grant {capability!r} at spawn, and can only do so if it "
                    "holds it too."
                )
            else:
                remedy = (
                    f"grant it in {self.perms.path or 'the permissions config'} "
                    "(agent grant / agent revoke)."
                )
            raise PermissionDenied(
                f"permission denied: {proc.name} does not hold capability "
                f"{capability!r} — {remedy}"
            )
        driver = self._driver(capability)

        self._yield_slot(proc, AgentState.WAITING, waiting_on=f"tool {capability}")
        key = dg.key(dg.TOOL, f"{proc.pid}.{call.req_id}")
        self.deps.add(proc.pid, call.req_id, {key})
        self._io_calls += 1
        task = asyncio.create_task(
            self._run_tool(proc, key, driver, capability, op, params)
        )
        self._io_tasks.add(task)
        task.add_done_callback(self._io_tasks.discard)

    async def _run_tool(
        self,
        proc: AgentProcess,
        key: str,
        driver: Any,
        capability: str,
        op: str,
        params: dict[str, Any],
    ) -> None:
        started = asyncio.get_running_loop().time()
        try:
            value = await driver.execute(op, params)
            try:
                assert_serializable(f"{capability}.{op} result", value)
                result = {"value": value, "error": None}
            except NotSerializable as exc:
                result = {"value": None, "error": str(exc)}
        except ToolError as exc:
            result = {"value": None, "error": str(exc)}
        except Exception as exc:  # a driver bug must not take the kernel down
            result = {"value": None, "error": f"{type(exc).__name__}: {exc}"}
        finally:
            self._io_calls -= 1

        elapsed = asyncio.get_running_loop().time() - started
        ok = result["error"] is None
        # p.8: the shared runtime knows all tool usage, the same way it knows
        # all model usage. One row per dispatch, whoever's application it was.
        self.store.record_tool_call(
            pid=proc.pid, agent=proc.name, capability=capability, op=op,
            latency=elapsed, ok=ok, error=result["error"],
        )
        self._log(
            proc.pid,
            "tool",
            f"{capability}.{op} {'completed' if ok else 'failed'} in {elapsed:.2f}s"
            + ("" if ok else f": {result['error']}"),
        )
        self.publish(
            "ToolCompleted",
            source_pid=proc.pid,
            pid=proc.pid,
            capability=capability,
            op=op,
            ok=ok,
        )
        for w in self.deps.resolve(key, result):
            self._wake_waiter(w)

    def _driver(self, capability: str) -> Any:
        """One driver instance per capability, created on first use."""
        if capability not in self._drivers:
            cls = REGISTRY.get(capability)
            if cls is None:
                raise ValueError(
                    f"no driver for capability {capability!r} "
                    f"(have: {', '.join(sorted(REGISTRY))})"
                )
            self._drivers[capability] = cls(
                log=lambda message: self._log(None, "driver", message),
                publish=lambda event_type, **payload: self.publish(
                    event_type, **payload
                ),
                **self.tools_config.get(capability, {}),
            )
        return self._drivers[capability]

    def _sys_memory(
        self,
        proc: AgentProcess,
        op: str,
        kind: str = "working",
        key: str | None = None,
        value: Any = None,
        with_agent: Any = "*",
        query: str | None = None,
        top: int = 3,
        limit: int = 20,
    ) -> Any:
        """The p.6 memory API. Fast, local, nonblocking: the agent keeps its
        slot. Shared-memory changes are announced as MemoryUpdated events."""
        if op == "store":
            self.memory.store_value(proc, key, value, kind)
            if kind == "shared":
                self.publish(
                    "MemoryUpdated", source_pid=proc.pid,
                    key=key, kind=kind, by=proc.name,
                )
            return None
        if op == "retrieve":
            return self.memory.retrieve(
                proc, key=key, kind=kind, query=query, top=top, limit=limit
            )
        if op == "share":
            self.memory.share(proc, key, with_agent)
            self.publish(
                "MemoryUpdated", source_pid=proc.pid,
                key=key, kind="shared", by=proc.name,
            )
            return None
        if op == "delete":
            deleted = self.memory.delete(proc, key, kind)
            if deleted and kind == "shared":
                self.publish(
                    "MemoryUpdated", source_pid=proc.pid,
                    key=key, kind=kind, by=proc.name, deleted=True,
                )
            return deleted
        raise ValueError(f"unknown memory op {op!r}")

    def _sys_request_model(
        self,
        proc: AgentProcess,
        call: Syscall,
        need: str,
        prompt: str,
        system: str | None,
        max_tokens: int,
    ) -> None:
        """Route a model request by capability class (p.7). The agent never
        names a model; the manager selects by availability, and the kernel
        records tokens and cost against this agent."""
        if not isinstance(need, str) or not need.strip():
            raise ValueError("request_model() needs a capability class, e.g. 'fast'")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("request_model() needs a non-empty prompt")

        self._yield_slot(proc, AgentState.WAITING, waiting_on=f"model {need}")
        key = dg.key(dg.MODEL, f"{proc.pid}.{call.req_id}")
        self.deps.add(proc.pid, call.req_id, {key})
        self._io_calls += 1
        task = asyncio.create_task(
            self._run_model(proc, key, need, prompt, system, max_tokens)
        )
        self._io_tasks.add(task)
        task.add_done_callback(self._io_tasks.discard)

    async def _run_model(
        self,
        proc: AgentProcess,
        key: str,
        need: str,
        prompt: str,
        system: str | None,
        max_tokens: int,
    ) -> None:
        started = asyncio.get_running_loop().time()
        try:
            value = await self.models.request(need, prompt, system, max_tokens)
            result = {"value": value, "error": None}
        except ModelError as exc:
            result = {"value": None, "error": str(exc)}
        except Exception as exc:  # a provider bug must not take the kernel down
            result = {"value": None, "error": f"{type(exc).__name__}: {exc}"}
        finally:
            self._io_calls -= 1

        elapsed = asyncio.get_running_loop().time() - started
        ok = result["error"] is None
        value = result["value"] or {}
        if ok and value.get("model"):
            proc.model = value["model"]  # the p.3 card's "Model:" line
        self.store.record_model_call(
            pid=proc.pid,
            agent=proc.name,
            need=need,
            model=value.get("model", "-"),
            input_tokens=value.get("input_tokens", 0),
            output_tokens=value.get("output_tokens", 0),
            cost=value.get("cost", 0.0),
            latency=elapsed,
            ok=ok,
            error=result["error"],
        )
        self._log(
            proc.pid,
            "model",
            f"need {need!r} served by {value['model']} "
            f"({value['input_tokens']}+{value['output_tokens']} tokens, "
            f"${value['cost']:.4f}, {elapsed:.2f}s)"
            if ok
            else f"need {need!r} failed in {elapsed:.2f}s: {result['error']}",
        )
        self.publish(
            "ModelFinished",
            source_pid=proc.pid,
            pid=proc.pid,
            need=need,
            model=value.get("model"),
            cost=value.get("cost", 0.0),
            ok=ok,
        )
        for w in self.deps.resolve(key, result):
            self._wake_waiter(w)

    def _sys_request_approval(
        self, proc: AgentProcess, call: Syscall, role: str, reason: str
    ) -> None:
        """Block until a human with `role` approves (p.5-6).

        The human is registered as a dependency-graph node, identical in kind
        to an agent, an event, or a timer. The approval object itself lives in
        the store, so it outlives this runtime.
        """
        if not isinstance(role, str) or not role.strip():
            raise ValueError("request_approval() needs a non-empty role")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("request_approval() needs a non-empty reason")

        row = self.store.request_approval(proc.name, role, reason, proc.pid)
        self._yield_slot(proc, AgentState.BLOCKED, waiting_on=role)
        key = dg.key(dg.APPROVAL, row["id"])

        if row["status"] == "granted":
            # Granted before we asked — e.g. while the runtime was down. It is
            # honored and consumed, and still costs a queue trip: an approval
            # is scheduling, not a shortcut.
            self.store.consume_approval(row["id"])
            self._log(proc.pid, "approval", f"{role} had already approved: {reason}")
            self._requeue(
                proc,
                Reply(req_id=call.req_id, value=_shape({key: _approval_value(row)})),
            )
            return

        self._log(
            proc.pid,
            "approval",
            f"{proc.name} blocked: needs {role} to approve ({reason})",
        )
        self.deps.add(proc.pid, call.req_id, {key})

    def _drain_approvals(self) -> None:
        """Wake whoever a granted approval unblocks.

        Grants arrive through the store — `agent approve` writes them there
        directly, possibly while we were not even running — so the kernel polls
        for them the same way it polls for control commands.
        """
        for row in self.store.granted_approvals():
            key = dg.key(dg.APPROVAL, row["id"])
            freed = self.deps.resolve(key, _approval_value(row))
            if not freed:
                continue  # a pre-grant: nobody in this runtime is blocked on it
            self.store.consume_approval(row["id"])
            self._log(row["pid"], "approval", f"{row['role']} approved: {row['reason']}")
            self.publish(
                "HumanApproved",
                pid=row["pid"],
                role=row["role"],
                reason=row["reason"],
            )
            for w in freed:
                self._wake_waiter(w)

    def _on_dep_timer(self, proc: AgentProcess, timer_key: str) -> None:
        # Clear the handle on the owner even if other dependencies are still
        # outstanding, so a later _cancel_timer cannot decrement _timers twice.
        self._timers -= 1
        proc.timer = None
        for w in self.deps.resolve(timer_key, True):
            self._wake_waiter(w)

    # -- process exit ----------------------------------------------------
    def _on_finish(self, proc: AgentProcess, result: Any) -> None:
        try:
            # A result crosses the boundary too: parents receive it via
            # wait(), and recovery re-serves it after a crash.
            assert_serializable(f"{proc.name} result", result)
        except NotSerializable as exc:
            self._on_fail(proc, exc)
            return
        proc.result = result
        proc.exit_reason = "completed"
        self.running.discard(proc.pid)
        self._cancel_timer(proc)
        self.table.transition(proc, AgentState.FINISHED)
        self._log(proc.pid, "exit", f"{proc.name} finished")
        self._announce_exit(proc)

    def _on_fail(self, proc: AgentProcess, exc: BaseException) -> None:
        killed = isinstance(exc, asyncio.CancelledError)
        if killed:
            # kill() and the deadlock detector set a reason before cancelling;
            # "killed" is only the fallback for a bare cancellation.
            proc.exit_reason = proc.exit_reason or "killed"
        else:
            proc.exit_reason = f"{type(exc).__name__}: {exc}"
        self.running.discard(proc.pid)
        self._cancel_timer(proc)
        self._discard_ready(proc)
        self.deps.cancel(proc.pid)
        if proc.state not in TERMINAL:
            self.table.transition(proc, AgentState.FAILED)
        # The failure is real and recorded before a restart is considered:
        # what a retry does is come back out of Failed, not skip it.
        if not killed and self._retry(proc):
            return
        self._log(proc.pid, "exit", f"{proc.name} {proc.exit_reason}")
        self._announce_exit(proc)

    def _retry(self, proc: AgentProcess) -> bool:
        """Restart a crashed agent, up to its budget (p.4: retries are a
        scheduler responsibility).

        The agent is re-created from its spec and replays its journal, so a
        retry costs only the work after its last completed syscall — the same
        machinery crash recovery uses, applied to one process instead of the
        whole runtime. A killed agent is never retried: a human said stop.
        Off by default; `Kernel(retries=N)` or `Agent.retries` opts in.
        """
        budget = getattr(self.agents.get(proc.pid), "retries", None)
        if budget is None:
            budget = self.retries
        if proc.retries >= budget:
            return False
        proc.retries += 1
        self._log(
            proc.pid, "retry",
            f"{proc.name} failed ({proc.exit_reason}); "
            f"restart {proc.retries}/{budget}",
        )
        self._journals[proc.pid] = self.store.load_journals().get(proc.pid, [])
        proc.exit_reason = None
        proc.ended_at = None
        proc.pending = None
        proc.current_op = None
        # A dead task must not be mistaken for a live one: _admit starts a
        # fresh task when this is None, and delivers an owed reply when it
        # is not. A restart needs the former.
        proc.task = None
        self.agents[proc.pid] = self._create_agent(proc.spec)
        self.table.transition(proc, AgentState.READY, waiting_on=None)
        self.ready.append(proc)
        self._nudge()
        return True

    def _announce_exit(self, proc: AgentProcess) -> None:
        """A terminated agent is an event and a resolved dependency (p.5)."""
        self._nudge()  # a process just went terminal: re-evaluate immediately
        self.bus.forget(proc.pid)
        self.memory.forget_process(proc.pid)  # private memory dies with the pid
        self.perms.forget_process(proc.pid)  # so does anything delegated to it
        finished = proc.state is AgentState.FINISHED
        self.publish(
            "AgentFinished" if finished else "AgentFailed",
            source_pid=proc.pid,
            pid=proc.pid,
            name=proc.name,
            **({"result": proc.result} if finished else {"reason": proc.exit_reason}),
        )
        for w in self.deps.resolve(dg.key(dg.AGENT, proc.pid), proc.result):
            self._wake_waiter(w)

    # -- deadlock (p.4: the scheduler must not simply hang) ---------------
    def _detect_deadlock(self) -> None:
        """Nobody can run, nobody is asleep, no timer is pending: nothing will
        ever happen again. Say so, instead of hanging forever."""
        if self.daemon_mode:
            return  # a daemon can always receive new work that publishes the event
        alive = [p for p in self.table.all() if p.alive]
        if not alive or self._timers > 0 or self._io_calls > 0:
            return  # a pending timer, tool, or model call can still wake someone
        if any(p.state in LIVE for p in alive):
            return

        stuck = [p for p in alive if p.state is AgentState.WAITING]
        detail = ", ".join(f"pid {p.pid} ({p.name}) waiting on {p.waiting_on}" for p in stuck)
        self._log(None, "deadlock", f"no runnable process: {detail}")
        for proc in stuck:
            self.deps.cancel(proc.pid)
            proc.exit_reason = f"deadlock: waiting on {proc.waiting_on}, nothing can satisfy it"
            if proc.task is not None and not proc.task.done():
                proc.task.cancel()
            else:
                self.table.transition(proc, AgentState.FAILED)

    # -- plumbing --------------------------------------------------------
    def _quiescent(self) -> bool:
        return not any(p.alive for p in self.table.all())

    def _discard_ready(self, proc: AgentProcess) -> None:
        try:
            self.ready.remove(proc)
        except ValueError:
            pass

    def _cancel_timer(self, proc: AgentProcess) -> None:
        if proc.timer is not None:
            proc.timer.cancel()
            proc.timer = None
            self._timers = max(0, self._timers - 1)

    def _drain_commands(self) -> None:
        for op, args in self.store.take_commands():
            try:
                {"kill": self.kill, "pause": self.pause, "resume": self.resume}[op](
                    **args
                )
            except KeyError:
                self._log(None, "error", f"unknown control command: {op}")
            except Exception as exc:
                self._log(None, "error", f"{op} failed: {exc}")

    def _publish_row(self, proc: AgentProcess, frm: Any = None, to: Any = None) -> None:
        self.store.publish(proc.row())
        if frm is not None:
            self._log(proc.pid, "state", f"{frm.value} -> {to.value}")

    def _log(self, pid: int | None, kind: str, message: str) -> None:
        self.store.append_log(pid, kind, message)


def _shape(results: dict[str, Any]) -> dict[str, Any]:
    """Turn flat dependency keys back into the shape the agent asked for."""
    out: dict[str, Any] = {
        "agents": {},
        "events": {},
        "timer": False,
        "approval": None,
        "tool": None,
        "model": None,
    }
    for k, v in results.items():
        kind, _, name = k.partition(":")
        if kind == dg.AGENT:
            out["agents"][int(name)] = v
        elif kind == dg.EVENT:
            out["events"][name] = v
        elif kind == dg.TIMER:
            out["timer"] = True
        elif kind == dg.APPROVAL:
            out["approval"] = v
        elif kind == dg.TOOL:
            out["tool"] = v
        elif kind == dg.MODEL:
            out["model"] = v
    return out


def _approval_value(row: dict[str, Any]) -> dict[str, Any]:
    """What the blocked agent receives once its approval is granted."""
    return {
        "role": row["role"],
        "reason": row["reason"],
        "by": row["resolved_by"] or row["role"],
    }


def _describe(deps: set[str], table: ProcessTable) -> str:
    if not deps:
        return "nothing"
    parts = []
    for k in sorted(deps):
        kind, _, name = k.partition(":")
        if kind == dg.AGENT:
            parts.append(f"pid {name} ({table.get(int(name)).name})")
        elif kind == dg.EVENT:
            parts.append(f"event {name}")
        elif kind == dg.TIMER:
            parts.append("timer")
    return ", ".join(parts)
