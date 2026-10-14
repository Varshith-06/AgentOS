"""One workload, each framework's own recovery path.

    python -m benchmarks.crashstudy.adapters <framework> start|resume

The workload is CS_CALLS billable calls in sequence, each preceded by CS_THINK
seconds of local work. `start` runs it from the beginning and is killed
partway; `resume` is what that framework's documentation says to do after a
crash, run to completion. With CS_KEYED=1 each call carries an idempotency key
derived from whatever stable identity the framework offers for that call.

Every adapter is written the way the framework's own examples write it: one
durable unit (node, activity, step, method, task) per call where the framework
has units, nothing cleverer.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
from pathlib import Path

from .client import calls, charge, keyed, note, think

RUN = os.environ.get("CS_RUN", "run")
DIR = Path(os.environ.get("CS_DIR", "."))


# --------------------------------------------------------------------------- agentos

def agentos(phase: str) -> None:
    from agentos import Kernel
    from agentos.drivers import REGISTRY, ToolDriver
    from agentos.kernel.store import Store

    from .agents import Caller

    class PaidAPI(ToolDriver):
        name = "paidapi"
        timeout = 60.0
        idempotent = ("charge",) if keyed() else ()

        async def op_charge(self, i: int, idempotency_key: str | None = None):
            return await asyncio.to_thread(charge, i, idempotency_key)

    REGISTRY["paidapi"] = PaidAPI
    k = Kernel(
        store=Store(DIR / "agentos"), tick=0.005, recover=phase == "resume",
        tools={"paidapi": {}}, permissions={"*": ["paidapi"]},
    )
    if phase == "start":
        k.spawn(Caller(calls=calls()))
    asyncio.run(k.run())


# ------------------------------------------------------------------------- langgraph

def _langgraph(phase: str, per_call: bool) -> None:
    from typing import TypedDict

    from langgraph.checkpoint.sqlite import SqliteSaver
    from langgraph.graph import END, START, StateGraph

    class S(TypedDict):
        done: int

    def key(i: int) -> str | None:
        return f"{RUN}:lg:{i}" if keyed() else None

    g = StateGraph(S)
    if per_call:
        def make(i: int):
            def node(state: S) -> S:
                think()
                charge(i, key(i))
                return {"done": i + 1}
            return node
        prev = START
        for i in range(calls()):
            g.add_node(f"c{i}", make(i))
            g.add_edge(prev, f"c{i}")
            prev = f"c{i}"
        g.add_edge(prev, END)
    else:
        def loop(state: S) -> S:
            for i in range(calls()):
                think()
                charge(i, key(i))
            return {"done": calls()}
        g.add_node("agent", loop)
        g.add_edge(START, "agent")
        g.add_edge("agent", END)

    conn = sqlite3.connect(DIR / "lg.db", check_same_thread=False)
    app = g.compile(checkpointer=SqliteSaver(conn))
    cfg = {"configurable": {"thread_id": RUN}, "recursion_limit": 100}
    # LangGraph's documented resume: invoke with None on the same thread.
    app.invoke({"done": 0} if phase == "start" else None, cfg)
    conn.close()


def langgraph_node(phase: str) -> None:
    _langgraph(phase, per_call=True)


def langgraph_loop(phase: str) -> None:
    _langgraph(phase, per_call=False)


# -------------------------------------------------------------------------- temporal

def temporal(phase: str) -> None:
    from temporalio.client import Client
    from temporalio.worker import Worker

    from . import temporal_defs as td

    async def go() -> None:
        client = await Client.connect(os.environ["CS_TEMPORAL"])
        if phase == "start":
            await client.start_workflow(
                td.Calls.run, calls(), id=RUN, task_queue=RUN)
        async with Worker(client, task_queue=RUN, workflows=[td.Calls],
                          activities=[td.billable]):
            await client.get_workflow_handle(RUN).result()

    asyncio.run(go())


# ------------------------------------------------------------------------------ dbos

def dbos(phase: str) -> None:
    from dbos import DBOS, DBOSConfig, SetWorkflowID

    config: DBOSConfig = {
        "name": "crashstudy",
        "system_database_url": f"sqlite:///{(DIR / 'dbos.sqlite').as_posix()}",
        "run_admin_server": False,
    }
    DBOS(config=config)

    @DBOS.step()
    def billable(i: int) -> dict:
        status = DBOS.step_status
        note(i, attempt=getattr(status, "current_attempt", None))
        think()
        k = f"{DBOS.workflow_id}:{DBOS.step_id}" if keyed() else None
        return charge(i, k)

    @DBOS.workflow()
    def work(n: int) -> str:
        for i in range(n):
            billable(i)
        return "done"

    # DBOS's documented recovery: launch() resumes this executor's pending
    # workflows on its own.
    DBOS.launch()
    if phase == "start":
        with SetWorkflowID(RUN):
            work(calls())
    else:
        DBOS.retrieve_workflow(RUN).get_result()
    DBOS.destroy()


# ---------------------------------------------------------------------------- crewai

def crewai(phase: str, guarded: bool = False) -> None:
    from crewai.flow.flow import Flow, listen, start
    from crewai.flow.persistence import persist
    from crewai.flow.persistence.sqlite import SQLiteFlowPersistence
    from pydantic import BaseModel

    class S(BaseModel):
        id: str = RUN
        done: int = 0

    def make(i: int):
        def step(self):
            if guarded and self.state.done > i:
                return  # restored state says this one already happened
            think()
            charge(i, f"{self.state.id}:c{i}" if keyed() else None)
            self.state.done = i + 1
        step.__name__ = f"c{i}"
        return step

    body, prev = {}, None
    for i in range(calls()):
        fn = make(i)
        body[f"c{i}"] = start()(fn) if prev is None else listen(prev)(fn)
        prev = body[f"c{i}"]
    cls = persist(SQLiteFlowPersistence(db_path=str(DIR / "flow.db")))(
        type("Calls", (Flow[S],), body))
    # CrewAI's documented resume: kickoff with the persisted state's id.
    cls().kickoff(inputs={"id": RUN})


def crewai_guarded(phase: str) -> None:
    """The same flow, each step skipping itself when restored state shows it done."""
    crewai(phase, guarded=True)


def crewai_checkpoint(phase: str) -> None:
    """The same flow under CrewAI's checkpointing (CheckpointConfig), not @persist.

    A checkpoint is written after every method finishes; resume restores the
    newest one with Flow.from_checkpoint, which carries the completed methods.
    """
    from crewai import CheckpointConfig
    from crewai.flow.flow import Flow, listen, start
    from pydantic import BaseModel

    class S(BaseModel):
        id: str = RUN
        done: int = 0

    def make(i: int):
        def step(self):
            think()
            charge(i, f"{RUN}:c{i}" if keyed() else None)
            self.state.done = i + 1
        step.__name__ = f"c{i}"
        return step

    body, prev = {}, None
    for i in range(calls()):
        fn = make(i)
        body[f"c{i}"] = start()(fn) if prev is None else listen(prev)(fn)
        prev = body[f"c{i}"]
    cls = type("Calls", (Flow[S],), body)
    where = DIR / "checkpoints"
    config = dict(location=str(where), on_events=["method_execution_finished"])
    # Checkpoints are JSON files under a branch directory, newest last; a kill
    # can tear the newest, so take the newest that parses.
    saved = sorted(where.rglob("*.json"), key=lambda p: p.stat().st_mtime) if where.exists() else []
    intact = None
    for path in reversed(saved):
        try:
            json.loads(path.read_text(encoding="utf-8"))
            intact = path
            break
        except ValueError:
            continue
    if phase == "resume" and intact:
        cls.from_checkpoint(CheckpointConfig(**config, restore_from=str(intact))).kickoff()
    else:
        cls(checkpoint=CheckpointConfig(**config)).kickoff()


# ---------------------------------------------------------------------------- prefect

def prefect(phase: str) -> None:
    """Prefect's documented recovery: persisted results plus an input cache policy.

    Re-running the flow returns cached results for tasks that completed.
    """
    from prefect import flow, task
    from prefect.cache_policies import INPUTS

    @task(cache_policy=INPUTS, persist_result=True)
    def billable(run: str, i: int) -> dict:
        think()
        return charge(i, f"{run}:pf:{i}" if keyed() else None)

    @flow(persist_result=True)
    def work(run: str, n: int) -> str:
        for i in range(n):
            billable(run, i)
        return "done"

    work(RUN, calls())


# ---------------------------------------------------------------------------- autogen

def autogen(phase: str) -> None:
    """AutoGen has no durable execution; its documented tool is save/load_state."""
    from autogen_core import AgentId, SingleThreadedAgentRuntime

    from . import autogen_defs as ad

    async def go() -> None:
        runtime = SingleThreadedAgentRuntime()
        ad.RUNTIME = runtime
        await ad.Worker.register(runtime, "worker", lambda: ad.Worker())
        if phase == "resume" and ad.STATE.exists():
            await runtime.load_state(json.loads(ad.STATE.read_text()))
        runtime.start()
        await runtime.send_message(ad.Go(n=calls()), AgentId("worker", "default"))
        await runtime.stop_when_idle()

    asyncio.run(go())


FRAMEWORKS = {
    "agentos": agentos,
    "langgraph-node": langgraph_node,
    "langgraph-loop": langgraph_loop,
    "temporal": temporal,
    "dbos": dbos,
    "crewai": crewai,
    "crewai-guarded": crewai_guarded,
    "crewai-checkpoint": crewai_checkpoint,
    "prefect": prefect,
    "autogen": autogen,
}

if __name__ == "__main__":
    FRAMEWORKS[sys.argv[1]](sys.argv[2])
