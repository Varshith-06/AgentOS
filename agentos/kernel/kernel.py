"""The kernel loop.

One event loop, one process table, one scheduler. Agents run only when the
scheduler hands them a slot, and they re-enter the ready queue every time they
give one up — a woken agent does not resume instantly, it queues like everyone
else. That is the difference between a scheduler and a callback.

Phase 2 adds the two things that make the order emergent rather than written
down: agents publish events instead of calling each other, and they wait on a
dependency graph instead of a sequence. Nothing in here knows what a workflow
is.
"""

from __future__ import annotations

import asyncio
import importlib
from collections import deque
from typing import Any

from ..agents.base import Agent, spec_of
from ..runtime.executor import Executor
from . import depgraph as dg
from .depgraph import DependencyGraph
from .events import EventBus
from .messages import Reply, Syscall
from .process import AgentProcess, ProcessTable
from .scheduler import SchedulerView, get_policy
from .states import TERMINAL, AgentState
from .store import Store

#: Syscalls that keep the agent's slot (it never stops running).
NONBLOCKING = {"spawn", "log", "publish", "subscribe"}

#: States from which the system can still make progress on its own.
LIVE = frozenset(
    {
        AgentState.RUNNING,
        AgentState.READY,
        AgentState.SLEEPING,
        AgentState.SUSPENDED,  # a human can resume it
        AgentState.BLOCKED,  # a human can approve it (Phase 3)
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
    ) -> None:
        self.table = ProcessTable()
        self.policy = get_policy(policy)
        self.slots = slots
        self.tick = tick
        self.store = store if store is not None else Store()

        self.mailbox: asyncio.Queue[Syscall] = asyncio.Queue()
        self.ready: deque[AgentProcess] = deque()
        self.running: set[int] = set()
        self.agents: dict[int, Agent] = {}

        self.bus = EventBus()
        self.deps = DependencyGraph()

        self.executor = Executor(self.mailbox, self._on_finish, self._on_fail)
        self.table.on_transition = self._publish_row
        self._shutdown = False
        self._timers = 0  # outstanding timer dependencies

        self.store.register_runtime(self.policy.name, slots)

    # -- public API (AgentOS.pdf p.9) ------------------------------------
    def spawn(self, agent: Agent, parent: int | None = None) -> int:
        proc = self.table.create(
            name=agent.name,
            spec=spec_of(agent),
            parent=parent,
            priority=getattr(agent, "priority", "Normal"),
        )
        self.agents[proc.pid] = agent
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
            self._cancel_timer(proc)
            self.table.transition(proc, AgentState.SUSPENDED, waiting_on="resume")
            proc.pause_requested = False
            self._log(pid, "pause", f"{proc.name} suspended")

    def resume(self, pid: int) -> None:
        proc = self.table.get(pid)
        proc.pause_requested = False
        if proc.state is not AgentState.SUSPENDED:
            return
        self.table.transition(proc, AgentState.READY)
        self.ready.append(proc)
        self._log(pid, "resume", f"{proc.name} resumed")

    def publish(self, event_type: str, source_pid: int | None = None, **payload: Any):
        """Announce an event. The bus decides who hears it (p.5)."""
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
        # Anyone whose dependency this satisfies becomes runnable.
        for w in self.deps.resolve(dg.key(dg.EVENT, event_type), payload):
            self._wake_waiter(w)
        return event

    async def run(self) -> None:
        """Run until every process is terminal — or until nothing can progress."""
        while not self._shutdown:
            self._drain_commands()
            await self._drain_mailbox()
            self._admit()
            self.store.heartbeat()
            if self._quiescent():
                break
            self._detect_deadlock()
            await asyncio.sleep(self.tick)

    async def run_until_done(self, agent: Agent) -> Any:
        pid = self.spawn(agent)
        await self.run()
        return self.table.get(pid).result

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
        """Owe `proc` a reply and put it back in line for a slot."""
        if not proc.alive or proc.state is AgentState.SUSPENDED:
            return
        proc.pending = reply
        self.table.transition(proc, AgentState.READY, waiting_on=None)
        self.ready.append(proc)

    def _wake_waiter(self, w: dg.Waiting) -> None:
        """A dependency set is fully satisfied: the scheduler wakes the waiter."""
        proc = self.table.get(w.pid)
        self._requeue(proc, Reply(req_id=w.req_id, value=_shape(w.results)))

    # -- syscalls --------------------------------------------------------
    async def _drain_mailbox(self) -> None:
        while not self.mailbox.empty():
            call = self.mailbox.get_nowait()
            proc = self.table.get(call.pid)
            try:
                self._handle(proc, call)
            except Exception as exc:  # kernel refuses; the agent sees the error
                if proc.state is AgentState.WAITING:  # it already gave up its slot
                    self._requeue(proc, Reply(req_id=call.req_id, error=str(exc)))
                else:
                    proc.inbox.put_nowait(Reply(req_id=call.req_id, error=str(exc)))

    def _handle(self, proc: AgentProcess, call: Syscall) -> None:
        if call.op in NONBLOCKING:
            value = getattr(self, f"_sys_{call.op}")(proc, **call.args)
            proc.inbox.put_nowait(Reply(req_id=call.req_id, value=value))
            return
        getattr(self, f"_sys_{call.op}")(proc, call, **call.args)

    def _sys_spawn(self, proc: AgentProcess, spec: dict[str, Any]) -> int:
        module = importlib.import_module(spec["module"])
        cls = getattr(module, spec["qualname"])
        child = cls(**spec.get("params", {}))
        return self.spawn(child, parent=proc.pid)

    def _sys_log(self, proc: AgentProcess, message: str) -> None:
        self._log(proc.pid, "agent", message)
        return None

    def _sys_publish(
        self, proc: AgentProcess, event_type: str, payload: dict[str, Any]
    ) -> None:
        self.publish(event_type, source_pid=proc.pid, **payload)
        return None

    def _sys_subscribe(self, proc: AgentProcess, event_types: list[str]) -> None:
        for event_type in event_types:
            self.bus.subscribe(proc.pid, event_type)
            self._log(proc.pid, "sub", f"{proc.name} subscribed to {event_type}")
        return None

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
                resolved[dg.key(dg.EVENT, event_type)] = buffered.payload
            else:
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

    def _on_dep_timer(self, proc: AgentProcess, timer_key: str) -> None:
        # Clear the handle on the owner even if other dependencies are still
        # outstanding, so a later _cancel_timer cannot decrement _timers twice.
        self._timers -= 1
        proc.timer = None
        for w in self.deps.resolve(timer_key, True):
            self._wake_waiter(w)

    # -- process exit ----------------------------------------------------
    def _on_finish(self, proc: AgentProcess, result: Any) -> None:
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
        self._log(proc.pid, "exit", f"{proc.name} {proc.exit_reason}")
        self._announce_exit(proc)

    def _announce_exit(self, proc: AgentProcess) -> None:
        """A terminated agent is an event and a resolved dependency (p.5)."""
        self.bus.forget(proc.pid)
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
        alive = [p for p in self.table.all() if p.alive]
        if not alive or self._timers > 0:
            return
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
    out: dict[str, Any] = {"agents": {}, "events": {}, "timer": False}
    for k, v in results.items():
        kind, _, name = k.partition(":")
        if kind == dg.AGENT:
            out["agents"][int(name)] = v
        elif kind == dg.EVENT:
            out["events"][name] = v
        elif kind == dg.TIMER:
            out["timer"] = True
    return out


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
