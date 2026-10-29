"""The kernel loop: one event loop, one process table, one scheduler."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections import deque
from typing import Any

from ..agents.base import Agent, agent_from_spec, spec_of
from ..drivers import REGISTRY, ToolError
from ..drivers.mcp import McpDriver, shadowing
from ..runtime.courier import CourierLost, Couriers
from ..runtime.subproc import ProcessExecutor
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

NONBLOCKING = {"spawn", "log", "publish", "subscribe", "memory", "checkpoint"}
AMBIGUITY_POLICIES = ("retry", "fail")


class AmbiguousOutcome(Exception):
    """A call was in flight at a crash and nothing recorded whether it ran."""

LIVE = frozenset(
    {
        AgentState.RUNNING,
        AgentState.READY,
        AgentState.SLEEPING,
        AgentState.SUSPENDED,
        AgentState.BLOCKED,
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
        retries: int = 0,
        daemon: bool = False,
        on_ambiguous: str = "retry",
        adopt: bool = True,
        courier: bool = False,
    ) -> None:
        if on_ambiguous not in AMBIGUITY_POLICIES:
            raise ValueError(
                f"on_ambiguous must be one of {', '.join(AMBIGUITY_POLICIES)}"
            )
        self.on_ambiguous = on_ambiguous
        self.table = ProcessTable()
        self.policy = get_policy(policy)
        self.slots = slots
        self.tick = tick
        self.retries = retries
        self.store = store if store is not None else Store()

        self.perms = Permissions.of(permissions, self.store.dir / "permissions.json")
        self.tools_config = tools or {}
        self._drivers: dict[str, Any] = {}
        self._io_tasks: set[asyncio.Task] = set()
        self._io_calls = 0  # running tool/model calls: pending I/O is not a deadlock

        self.memory = MemoryManager(self.store)
        self.models = ModelManager.of(
            models,
            self.store.dir / "models.json",
            log=lambda message: self._log(None, "model", message),
        )

        self.mailbox: asyncio.Queue[Syscall] = asyncio.Queue()
        self._early: deque[Syscall] = deque()
        self._last_poll = 0.0
        self._wakeup: asyncio.Event | None = None
        self.ready: deque[AgentProcess] = deque()
        self.running: set[int] = set()
        self.agents: dict[int, Agent] = {}

        self.bus = EventBus()
        self.deps = DependencyGraph()

        self.transport = ProcessExecutor.transport
        self.executor = ProcessExecutor(self.mailbox, self._on_finish, self._on_fail)
        # Daemon mode: never exit on quiescence, never call a stall a deadlock.
        self.daemon_mode = daemon
        self.table.on_transition = self._publish_row
        self._shutdown = False
        self._timers = 0  # outstanding timer dependencies

        # A call whose agent died while the kernel lived is still the kernel's:
        # it runs to the end, and the restarted agent is given its reply instead
        # of making it again. False restores re-issue-and-flag.
        self.adopt = adopt
        self._inflight: dict[int, dict[str, Any]] = {}
        # Couriers carry calls in processes of their own, so a reply outlives
        # even the runtime (agentos.runtime.courier).
        self.couriers = (
            Couriers(self.store.dir, log=lambda message: self._log(None, "courier", message))
            if courier else None
        )
        if self.couriers is not None and not recover:
            self.couriers.reset()

        self._recovered = recover
        if recover:
            self.store.resume_runtime(self.policy.name, slots)
            self._journals = self.store.load_journals()
            self._ambiguous = self.store.open_intents()
            self._restore()
        else:
            self.store.register_runtime(self.policy.name, slots)
            self._journals = {}
            self._ambiguous = {}

    def _restore(self) -> None:
        """Rebuild the world from the store after a hard kill."""
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
        intent, proc.intent = proc.intent, None
        self.store.append_journal(proc.pid, req_id, op, value, error, intent=intent)
        proc.checkpoint += 1
        self.store.publish(proc.row())

    def _intend(
        self, proc: AgentProcess, call: Syscall, target: str, idempotent: bool
    ) -> str:
        """Write the intent before dispatch; return the idempotency key to send.

        A call that was in flight when the runtime died comes back here with the
        same req_id. It keeps its original key, so an idempotent target applies
        it at most once. Anything else is a call that may already have run, and
        the policy decides: re-issue it and say so, or refuse to guess.
        """
        prior = self._ambiguous.pop(proc.pid, None)
        key = uuid.uuid4().hex
        if prior is not None and prior["target"] != target:
            self._abandon(proc, prior)
        elif prior is not None:
            self.store.settle_intent(prior["id"])
            if idempotent:
                key = prior["key"]
                self._log(
                    proc.pid, "recover",
                    f"{target} #{call.req_id} was in flight at the crash; "
                    "re-issued under its original idempotency key",
                )
            else:
                self.publish(
                    "OutcomeAmbiguous", source_pid=proc.pid, pid=proc.pid,
                    target=target, req_id=call.req_id, policy=self.on_ambiguous,
                )
                if self.on_ambiguous == "fail":
                    self._log(
                        proc.pid, "ambiguous",
                        f"{target} #{call.req_id} may have run before the crash; "
                        "not re-issued",
                    )
                    raise AmbiguousOutcome(
                        f"{target} was in flight when the runtime crashed and may "
                        "or may not have taken effect. It was not re-issued; "
                        "check before repeating it."
                    )
                self._log(
                    proc.pid, "ambiguous",
                    f"{target} #{call.req_id} may have run before the crash; "
                    "re-issued anyway, so it may now have run twice",
                )
        proc.intent = self.store.append_intent(
            proc.pid, call.req_id, call.op, target, key
        )
        return key

    def _abandon(self, proc: AgentProcess, prior: dict[str, Any]) -> None:
        """The agent went live and asked for something else: the call stays unknown."""
        self.store.settle_intent(prior["id"])
        self._log(
            proc.pid, "recover",
            f"in-flight {prior['target']} #{prior['req_id']} was never asked "
            "for again; its outcome stays unknown",
        )

    def _replay_entry(self, proc: AgentProcess, call: Syscall) -> dict[str, Any] | None:
        """The journaled reply for this syscall, if we are replaying one."""
        entries = self._journals.get(proc.pid)
        if not entries:
            return None
        entry = entries[0]
        if entry["req_id"] != call.req_id or entry["op"] != call.op:
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
            proc.task.cancel()
        else:
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
        """Satisfy a pending approval (p.6). Refused unless `role` matches."""
        self.store.approve(pid, role)
        self._drain_approvals()

    def publish(self, event_type: str, source_pid: int | None = None, **payload: Any):
        """Announce an event. The bus decides who hears it (p.5)."""
        # Mid-wait subscribers receive this publish through dependency resolution,
        # so the copy buffered for them must be consumed too.
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
        for w in self.deps.resolve(dg.key(dg.EVENT, event_type), payload):
            self._wake_waiter(w)
        return event

    async def run(self) -> None:
        """Run until every process is terminal — or until nothing can progress."""
        while not self._shutdown:
            now = time.time()
            polled = now - self._last_poll >= self.tick
            if polled:
                self._last_poll = now
                self.perms.refresh()
                self._drain_commands()
                self._drain_approvals()
                self.store.heartbeat()

            await self._drain_mailbox()
            self._admit()

            if self._quiescent() and not self.daemon_mode:
                break
            if polled:
                self._detect_deadlock()
            await self._idle(self.tick)
        for task in list(self._io_tasks):
            task.cancel()
        if self._io_tasks:
            await asyncio.gather(*self._io_tasks, return_exceptions=True)
        close = getattr(self.executor, "aclose", None)
        if close is not None:
            await close()
        if self.couriers is not None:
            self.couriers.release()
        for driver in self._drivers.values():
            # MCP servers are child processes; databases hold files open.
            close = getattr(driver, "close", None)
            if close is not None:
                await close()

    async def run_until_done(self, agent: Agent) -> Any:
        pid = self.spawn(agent)
        await self.run()
        return self.table.get(pid).result

    def submit_spec(
        self,
        spec: dict[str, Any],
        grant: list[str] | None = None,
        budget_usd: float | None = None,
    ) -> int:
        """Spawn from a serialized spec: how a thin client hands over an agent."""
        pid = self.spawn(self._create_agent(spec))
        proc = self.table.get(pid)
        if grant is not None:
            self.perms.assign(pid, set(grant))
            proc.permissions = sorted(grant)
            self._log(
                None, "grant",
                f"pid {pid} admitted with {', '.join(sorted(grant)) or 'nothing'}",
            )
        if budget_usd is not None:
            proc.budget_usd = float(budget_usd)
            self._log(None, "budget", f"pid {pid} admitted with ${budget_usd:.4f}")
        if grant is not None or budget_usd is not None:
            self._publish_row(proc)
        return pid

    def snapshot(self) -> dict[str, Any]:
        """One consistent view of the scheduler for the dashboard (p.8):"""
        return {
            "policy": self.policy.name,
            "slots": self.slots,
            "transport": self.transport,
            "gpu": gpu.summary(),
            "running": sorted(self.running),
            "ready": [p.pid for p in self.ready],
            "processes": [p.row() for p in self.table.all()],
            "deps": [
                {"pid": pid, "waits_on": sorted(w.remaining)}
                for pid, w in self.deps.waiting.items()
            ],
        }

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
                reply, proc.pending = proc.pending, None
                proc.inbox.put_nowait(reply)

    def _yield_slot(
        self, proc: AgentProcess, state: AgentState, waiting_on: str
    ) -> None:
        self.running.discard(proc.pid)
        self.table.transition(proc, state, waiting_on=waiting_on)

    def _requeue(self, proc: AgentProcess, reply: Reply) -> None:
        """Owe `proc` a reply and put it back in line for a slot."""
        if not proc.alive:
            return
        self._journal(proc, reply.req_id, proc.current_op or "?", reply.value, reply.error)
        proc.pending = reply
        self._nudge()
        if proc.state is AgentState.SUSPENDED:
            return
        self.table.transition(proc, AgentState.READY, waiting_on=None)
        self.ready.append(proc)

    def _wake_waiter(self, w: dg.Waiting) -> None:
        """A dependency set is fully satisfied: the scheduler wakes the waiter."""
        proc = self.table.get(w.pid)
        self._requeue(proc, Reply(req_id=w.req_id, value=_shape(w.results)))

    def _nudge(self) -> None:
        """Cut the current idle short: there is work the loop should see now."""
        if self._wakeup is not None:
            self._wakeup.set()

    async def _idle(self, timeout: float) -> None:
        """Doze until a syscall arrives, someone nudges, or `timeout` elapses."""
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
            except Exception as exc:
                if proc.state is AgentState.WAITING:
                    self._requeue(proc, Reply(req_id=call.req_id, error=str(exc)))
                else:
                    self._journal(proc, call.req_id, call.op, None, str(exc))
                    proc.inbox.put_nowait(Reply(req_id=call.req_id, error=str(exc)))

    def _handle(self, proc: AgentProcess, call: Syscall) -> None:
        entry = self._replay_entry(proc, call)
        if entry is not None:
            # Replaying: do not change the world twice. Subscribe is the exception --
            # it rebuilds kernel state that died with the old runtime.
            if call.op == "subscribe":
                self._sys_subscribe(proc, **call.args)
            proc.inbox.put_nowait(
                Reply(req_id=call.req_id, value=entry["value"], error=entry["error"])
            )
            return
        prior = self._ambiguous.get(proc.pid)
        if prior is not None and (prior["req_id"], prior["op"]) != (call.req_id, call.op):
            self._abandon(proc, self._ambiguous.pop(proc.pid))
        flight = self._inflight.get(proc.pid)
        if (
            flight is not None and flight.get("orphan") and not flight.get("abandoned")
            and (flight["req_id"], flight["op"]) != (call.req_id, call.op)
        ):
            # The restarted agent went another way: its old call's reply has
            # nobody to go to.
            flight["abandoned"] = True
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
        """Create a child: delegate capabilities to it, and wire its events."""
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
        """kernel.checkpoint(): the explicit form of what every syscall does anyway."""
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
            allowed = ", ".join(proc.publishes) or "nothing"
            raise InvalidEvent(
                f"{proc.name} was not wired to publish {event_type!r}; "
                f"it may publish: {allowed}"
            )
        self.publish(event_type, source_pid=proc.pid, **payload)
        return None

    def _unpublishable(self, event_type: str) -> bool:
        """Can we *prove* nobody will ever publish this event type?"""
        if event_type in KERNEL_EVENTS:
            return False
        for other in self.table.all():
            if not other.alive:
                continue
            if other.publishes is None:
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
        """Redeliver pre-crash events this subscriber was owed but never took."""
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
            self.bus.subscribe(proc.pid, event_type)
            buffered = self.bus.consume(proc.pid, event_type)
            if buffered is not None:
                self.store.record_consumption(proc.pid, buffered.seq)
                resolved[dg.key(dg.EVENT, event_type)] = buffered.payload
                continue
            if timer is None and self._unpublishable(event_type):
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

        if w.satisfied:
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
        """Dispatch a tool call through its driver (p.6-7)."""
        if not isinstance(capability, str) or not capability.strip():
            raise ValueError("request_tool() needs a capability name")
        if not self.perms.allowed(proc.name, capability, proc.pid):
            self._log(
                proc.pid,
                "denied",
                f"{proc.name} requested {capability!r}: permission denied",
            )
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
        target = f"{capability}.{op}"
        key = dg.key(dg.TOOL, f"{proc.pid}.{call.req_id}")
        if self._attach(proc, call, target, key, f"tool {capability}"):
            return
        adopted = self._recall(proc, call, target)
        idem = None
        if adopted is None:
            idem = self._intend(proc, call, target, op in getattr(driver, "idempotent", ()))
        via = None
        if adopted is None and op in getattr(driver, "courier", {}):
            send = self._courier(proc)
            if send is not None:
                via = lambda params, send=send, fn=driver.courier[op]: send(fn, params)  # noqa: E731

        self._yield_slot(proc, AgentState.WAITING, waiting_on=f"tool {capability}")
        self.deps.add(proc.pid, call.req_id, {key})
        flight = self._fly(proc, call, target, key)
        self._io_calls += 1
        task = asyncio.create_task(
            self._run_tool(proc, key, driver, capability, op, params, idem, adopted, via,
                           flight)
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
        idempotency_key: str | None = None,
        adopted: str | None = None,
        via: Any = None,
        flight: dict[str, Any] | None = None,
    ) -> None:
        started = asyncio.get_running_loop().time()
        try:
            if adopted is not None:
                value = await self.couriers.wait(adopted)
            else:
                value = await driver.execute(
                    op, params, idempotency_key=idempotency_key, via=via
                )
            try:
                assert_serializable(f"{capability}.{op} result", value)
                result = {"value": value, "error": None}
            except NotSerializable as exc:
                result = {"value": None, "error": str(exc)}
        except ToolError as exc:
            result = {"value": None, "error": str(exc)}
        except CourierLost as exc:
            self._lost(proc, f"{capability}.{op}")
            result = {"value": None, "error": str(exc)}
        except Exception as exc:
            result = {"value": None, "error": f"{type(exc).__name__}: {exc}"}
        finally:
            self._io_calls -= 1

        elapsed = asyncio.get_running_loop().time() - started
        ok = result["error"] is None
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
        self._deliver(proc, key, result, flight)

    def _driver(self, capability: str) -> Any:
        """One driver instance per capability, created on first use."""
        if capability not in self._drivers:
            cls = REGISTRY.get(capability)
            extra: dict[str, Any] = {}
            if cls is None and capability.startswith("mcp:") and capability in self.tools_config:
                # An MCP server: its pins live with the runtime, so they outlast a crash.
                cls = McpDriver
                extra = {"server": capability[4:],
                         "pins_path": self.store.dir / "mcp_pins.json"}
            if cls is None:
                mounted = [c for c in self.tools_config if c.startswith("mcp:")]
                raise ValueError(
                    f"no driver for capability {capability!r} "
                    f"(have: {', '.join(sorted(REGISTRY) + sorted(mounted))})"
                )
            self._drivers[capability] = cls(
                log=lambda message: self._log(None, "driver", message),
                publish=lambda event_type, **payload: self.publish(
                    event_type, **payload
                ),
                **extra,
                **self.tools_config.get(capability, {}),
            )
        return self._drivers[capability]

    def _sys_describe_tools(
        self, proc: AgentProcess, call: Syscall, capabilities: list[str]
    ) -> None:
        """What each held capability's ops take, as JSON Schema. Asking costs a
        server start for MCP, so it blocks like a tool call; it changes nothing."""
        held = [c for c in dict.fromkeys(capabilities or [])
                if isinstance(c, str) and self.perms.allowed(proc.name, c, proc.pid)]
        self._yield_slot(proc, AgentState.WAITING, waiting_on="tool descriptions")
        key = dg.key(dg.TOOL, f"{proc.pid}.{call.req_id}")
        self.deps.add(proc.pid, call.req_id, {key})
        self._io_calls += 1
        task = asyncio.create_task(self._describe(key, held))
        self._io_tasks.add(task)
        task.add_done_callback(self._io_tasks.discard)

    async def _describe(self, key: str, capabilities: list[str]) -> None:
        catalog: dict[str, Any] = {}
        try:
            for cap in capabilities:
                try:
                    catalog[cap] = await self._driver(cap).describe()
                except Exception as exc:
                    catalog[cap] = {"error": f"{type(exc).__name__}: {exc}"}
            servers = {c: {n: t for n, t in tools.items() if "refused" not in t}
                       for c, tools in catalog.items()
                       if isinstance(self._drivers.get(c), McpDriver)}
            for (cap, tool), why in shadowing(servers).items():
                if tool in self._drivers[cap].allow:
                    continue  # the operator vouched for it
                self._drivers[cap].refuse(tool, why)
                catalog[cap][tool] = {"refused": why}
            result = {"value": catalog, "error": None}
        except Exception as exc:
            result = {"value": None, "error": f"{type(exc).__name__}: {exc}"}
        finally:
            self._io_calls -= 1
        for w in self.deps.resolve(key, result):
            self._wake_waiter(w)

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
        """The memory API: fast, local, and nonblocking."""
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
        """Route a model request by capability class. The agent never names a model."""
        if not isinstance(need, str) or not need.strip():
            raise ValueError("request_model() needs a capability class, e.g. 'fast'")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("request_model() needs a non-empty prompt")

        over = self._over_budget(proc)
        if over is not None:
            spent, budget = over
            self._log(
                proc.pid, "budget",
                f"{proc.name} refused: task {proc.root} has spent "
                f"${spent:.6f} of its ${budget:.6f} budget",
            )
            raise ModelError(
                f"budget exhausted: this task has spent ${spent:.6f} of its "
                f"${budget:.6f} allowance. Finish with what you have."
            )

        # Behind a deduplicating gateway a model call is idempotent: the key in
        # its intent reaches the gateway, and a re-issue after a crash is
        # answered from the gateway's record instead of being billed again.
        idempotent = self.models.idempotent(need)
        target = f"model:{need}"
        key = dg.key(dg.MODEL, f"{proc.pid}.{call.req_id}")
        if self._attach(proc, call, target, key, f"model {need}"):
            return
        adopted = self._recall(proc, call, target)
        idem = courier = None
        if adopted is None:
            idem = self._intend(proc, call, target, idempotent=idempotent)
            courier = self._courier(proc)
        self._yield_slot(proc, AgentState.WAITING, waiting_on=f"model {need}")
        self.deps.add(proc.pid, call.req_id, {key})
        flight = self._fly(proc, call, target, key)
        self._io_calls += 1
        task = asyncio.create_task(
            self._run_model(proc, key, need, prompt, system, max_tokens,
                            idem if idempotent else None, adopted, courier, flight)
        )
        self._io_tasks.add(task)
        task.add_done_callback(self._io_tasks.discard)

    def _over_budget(self, proc: AgentProcess) -> tuple[float, float] | None:
        """Has this agent's *task* spent its allowance? (spent, budget) if so."""
        try:
            root = self.table.get(proc.root)
        except KeyError:
            return None
        budget = root.budget_usd
        if budget is None:
            return None
        costs = self.store.model_costs()
        spent = sum(
            entry["cost"]
            for pid, entry in costs.items()
            if pid in self.table._procs and self.table.get(pid).root == proc.root
        )
        return (spent, budget) if spent >= budget else None

    async def _run_model(
        self,
        proc: AgentProcess,
        key: str,
        need: str,
        prompt: str,
        system: str | None,
        max_tokens: int,
        idempotency_key: str | None = None,
        adopted: str | None = None,
        courier: Any = None,
        flight: dict[str, Any] | None = None,
    ) -> None:
        started = asyncio.get_running_loop().time()
        try:
            if adopted is not None:
                raw = await self.couriers.wait(adopted)
                value = self.models.result(
                    self.couriers.meta(adopted)["cand"], raw,
                    asyncio.get_running_loop().time() - started,
                )
            else:
                value = await self.models.request(
                    need, prompt, system, max_tokens,
                    idempotency_key=idempotency_key, courier=courier,
                )
            result = {"value": value, "error": None}
        except ModelError as exc:
            result = {"value": None, "error": str(exc)}
        except CourierLost as exc:
            self._lost(proc, f"model:{need}")
            result = {"value": None, "error": str(exc)}
        except Exception as exc:
            result = {"value": None, "error": f"{type(exc).__name__}: {exc}"}
        finally:
            self._io_calls -= 1

        elapsed = asyncio.get_running_loop().time() - started
        ok = result["error"] is None
        value = result["value"] or {}
        if ok and value.get("model"):
            proc.model = value["model"]
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
        if ok and value.get("replayed"):
            self._log(
                proc.pid, "recover",
                f"model:{need} answered from the gateway's record: "
                "the provider was not billed for it again",
            )
        if ok and value.get("outcome_unknown"):
            # The gateway lost track of an earlier attempt (it died with the
            # call in flight, or the provider failed): that one may be billed too.
            self.publish(
                "OutcomeAmbiguous", source_pid=proc.pid, pid=proc.pid,
                target=f"model:{need}", policy="gateway",
            )
            self._log(
                proc.pid, "ambiguous",
                f"model:{need} was sent again by a gateway that could not tell "
                "whether an earlier attempt was billed; it may have been billed twice",
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
        self._deliver(proc, key, result, flight)

    def _fly(
        self, proc: AgentProcess, call: Syscall, target: str, dep: str
    ) -> dict[str, Any] | None:
        """Note the call the kernel is now making for `proc`, in case `proc` dies."""
        if not self.adopt:
            return None
        flight = {
            "req_id": call.req_id, "op": call.op, "target": target,
            "dep": dep, "intent": proc.intent,
        }
        self._inflight[proc.pid] = flight
        return flight

    def _attach(
        self, proc: AgentProcess, call: Syscall, target: str, dep: str, waiting_on: str
    ) -> bool:
        """A restarted agent asking again for the call its last life left in
        flight: wait for that call instead of making it twice."""
        flight = self._inflight.get(proc.pid)
        if flight is None or not flight.get("orphan") or flight.get("abandoned"):
            return False
        if (flight["req_id"], flight["op"], flight["target"]) != (call.req_id, call.op, target):
            flight["abandoned"] = True  # asked for something else: not this call
            return False
        proc.intent = flight["intent"]  # settled when the reply is journaled
        self._yield_slot(proc, AgentState.WAITING, waiting_on=waiting_on)
        self.deps.add(proc.pid, call.req_id, {dep})
        self._log(
            proc.pid, "recover",
            f"{target} #{call.req_id} is still in flight from before the restart; "
            "waiting for it instead of making it again",
        )
        return True

    def _deliver(
        self, proc: AgentProcess, dep: str, result: dict[str, Any],
        flight: dict[str, Any] | None = None,
    ) -> None:
        """A tool or model call finished: wake whoever waits for it."""
        if flight is not None and self._inflight.get(proc.pid) is flight:
            del self._inflight[proc.pid]
        if flight is not None and flight.get("abandoned"):
            # Its dependency key may now name a different call: wake nobody.
            self._orphaned(proc, flight, result)
            return
        woken = self.deps.resolve(dep, result)
        for w in woken:
            self._wake_waiter(w)
        if not woken and flight is not None and flight.get("orphan"):
            self._orphaned(proc, flight, result)

    def _orphaned(
        self, proc: AgentProcess, flight: dict[str, Any], result: dict[str, Any]
    ) -> None:
        """The call outlived the agent that made it. Its reply goes in the
        journal, where the restarted agent's replay will find it."""
        target, req_id = flight["target"], flight["req_id"]
        if flight.get("abandoned") or not proc.alive:
            if flight["intent"] is not None:
                self.store.settle_intent(flight["intent"])
            self._log(
                proc.pid, "recover",
                f"{target} #{req_id} finished after its agent died, and was never "
                "asked for again; its reply is dropped",
            )
            return
        value = _shape({flight["dep"]: result})
        self.store.append_journal(
            proc.pid, req_id, flight["op"], value, None, intent=flight["intent"]
        )
        proc.checkpoint += 1
        self.store.publish(proc.row())
        self._journals.setdefault(proc.pid, []).append(
            {"req_id": req_id, "op": flight["op"], "value": value, "error": None}
        )
        self._log(
            proc.pid, "recover",
            f"{target} #{req_id} finished after its agent died; its reply is "
            "journaled for the restarted agent, so the call is not made again",
        )

    def _courier(self, proc: AgentProcess) -> Any:
        """send(target, args, meta) for `proc`'s current call, if couriers are on."""
        if self.couriers is None or proc.intent is None:
            return None
        intent, attempts = proc.intent, iter(range(1, 1 << 30))

        async def send(target: str, args: dict[str, Any], meta: Any = None) -> Any:
            return await self.couriers.call(intent, next(attempts), target, args, meta)

        return send

    def _recall(self, proc: AgentProcess, call: Syscall, target: str) -> str | None:
        """After a runtime crash: did this call's courier make it?

        Returns the courier's key if its reply is there, or will be, to take
        instead of calling again. A call whose courier never started is sent
        now without a flag. A courier that died mid-call is left to _intend,
        which flags the repeat as any lost call.
        """
        if self.couriers is None:
            return None
        prior = self._ambiguous.get(proc.pid)
        if prior is None or (prior["req_id"], prior["target"]) != (call.req_id, target):
            return None
        fate, row = self.couriers.fate(prior["id"])
        if fate == "lost":
            return None
        self._ambiguous.pop(proc.pid)
        if fate == "unsent":
            self.store.settle_intent(prior["id"])
            self._log(
                proc.pid, "recover",
                f"{target} #{call.req_id} was never made: its courier had not "
                "started when the runtime died. Making it now",
            )
            return None
        proc.intent = prior["id"]  # settled when the reply is journaled
        self._log(
            proc.pid, "recover",
            f"{target} #{call.req_id} was in flight at the crash; its courier "
            + ("had finished" if fate in ("done", "failed") else "is still at it")
            + ": taking its reply instead of making the call again",
        )
        return row["key"]

    def _lost(self, proc: AgentProcess, target: str) -> None:
        """A courier died mid-call: say so, as any call nobody can account for."""
        self.publish(
            "OutcomeAmbiguous", source_pid=proc.pid, pid=proc.pid,
            target=target, policy="courier",
        )
        self._log(
            proc.pid, "ambiguous",
            f"{target}: its courier died mid-call; it may or may not have taken effect",
        )

    def _sys_request_approval(
        self, proc: AgentProcess, call: Syscall, role: str, reason: str
    ) -> None:
        """Block until a human with `role` approves (p.5-6)."""
        if not isinstance(role, str) or not role.strip():
            raise ValueError("request_approval() needs a non-empty role")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("request_approval() needs a non-empty reason")

        row = self.store.request_approval(proc.name, role, reason, proc.pid)
        self._yield_slot(proc, AgentState.BLOCKED, waiting_on=role)
        key = dg.key(dg.APPROVAL, row["id"])

        if row["status"] == "granted":
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
        """Wake whoever a granted approval unblocks."""
        for row in self.store.granted_approvals():
            key = dg.key(dg.APPROVAL, row["id"])
            freed = self.deps.resolve(key, _approval_value(row))
            if not freed:
                continue
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
        # Cleared even while other dependencies are outstanding, so a later
        # _cancel_timer cannot decrement _timers twice.
        self._timers -= 1
        proc.timer = None
        for w in self.deps.resolve(timer_key, True):
            self._wake_waiter(w)

    def _on_finish(self, proc: AgentProcess, result: Any) -> None:
        try:
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
            proc.exit_reason = proc.exit_reason or "killed"
        else:
            proc.exit_reason = f"{type(exc).__name__}: {exc}"
        self.running.discard(proc.pid)
        self._cancel_timer(proc)
        self._discard_ready(proc)
        self.deps.cancel(proc.pid)
        if proc.state not in TERMINAL:
            self.table.transition(proc, AgentState.FAILED)
        if not killed and self._retry(proc):
            return
        self._log(proc.pid, "exit", f"{proc.name} {proc.exit_reason}")
        self._announce_exit(proc)

    def _retry(self, proc: AgentProcess) -> bool:
        """Restart a crashed agent, up to its retry budget."""
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
        journal = self.store.load_journals().get(proc.pid, [])
        # Drop the failed tail before replaying: a journaled failure replays as the
        # same failure forever. A failed syscall had no side effect to repeat.
        while journal and self._entry_failed(journal[-1]):
            journal.pop()
        self._journals[proc.pid] = journal
        # An agent that died mid-call left that call's intent open, exactly as a
        # runtime crash would: the restart must not re-issue it blind.
        intent = self.store.open_intents().get(proc.pid)
        flight = self._inflight.get(proc.pid)
        if flight is not None and intent is not None and flight["intent"] == intent["id"]:
            # Its agent is gone; the call is not. The restart waits for it.
            flight["orphan"] = True
            self._log(
                proc.pid, "recover",
                f"{flight['target']} #{flight['req_id']} is still in flight; the "
                "restarted agent will be given its reply, not make it again",
            )
        elif intent is not None:
            self._ambiguous[proc.pid] = intent
        proc.intent = None
        proc.exit_reason = None
        proc.ended_at = None
        proc.pending = None
        proc.current_op = None
        # _admit starts a fresh task when this is None and delivers an owed reply
        # when it is not; a restart needs the former.
        proc.task = None
        self.agents[proc.pid] = self._create_agent(proc.spec)
        self.table.transition(proc, AgentState.READY, waiting_on=None)
        self.ready.append(proc)
        self._nudge()
        return True

    @staticmethod
    def _entry_failed(entry: dict[str, Any]) -> bool:
        """Did this journaled syscall end in failure?"""
        if entry.get("error"):
            return True
        value = entry.get("value")
        if isinstance(value, dict):
            for kind in ("tool", "model"):
                inner = value.get(kind)
                if isinstance(inner, dict) and inner.get("error"):
                    return True
        return False

    def _announce_exit(self, proc: AgentProcess) -> None:
        """A terminated agent is an event and a resolved dependency (p.5)."""
        self._nudge()
        self.bus.forget(proc.pid)
        self.memory.forget_process(proc.pid)
        self.perms.forget_process(proc.pid)
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
        self._fail_orphaned_event_waits()

    def _fail_orphaned_event_waits(self) -> None:
        """Fail the event waits an exit has just made unsatisfiable."""
        prefix = f"{dg.EVENT}:"
        if not any(key.startswith(prefix) for key in self.deps.blocked_on):
            return
        for pid, w in list(self.deps.waiting.items()):
            waiter = self.table.get(pid)
            if not waiter.alive:
                continue
            for key in w.remaining:
                kind, _, name = key.partition(":")
                if kind != dg.EVENT or not self._unpublishable(name):
                    continue
                self.deps.cancel(pid)
                self._log(
                    pid, "deadlock",
                    f"wait on {name!r} orphaned: its last possible publisher exited",
                )
                self._requeue(waiter, Reply(
                    req_id=w.req_id,
                    error=(
                        f"nobody can publish {name!r} anymore: the last agent "
                        "wired for it exited without publishing. Its work may "
                        "have failed — check on your agents instead of waiting."
                    ),
                ))
                break

    def _detect_deadlock(self) -> None:
        """Nothing can run, sleep, or fire a timer: say so instead of hanging."""
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
