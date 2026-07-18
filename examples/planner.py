"""A task arrives as a sentence; the team is invented on the spot.

    python -m agentos.cli run examples/planner.py

Nothing here is predefined except the tools the operator allows. There is no
workflow graph, and no agent classes for "researcher" or "writer" — the
planner invents those roles at runtime, and AgentOS runs them as processes.

It is fully offline: the "model" is a scripted mock, so the sequence of
decisions is fixed and this demonstrates the *runtime*, not a model's
cleverness. Point the `fast` class at a real model and the same code runs a
real planner — that is the Phase 5 claim.

Watch it from another terminal:

    python -m agentos.cli ps       # the invented team, with their permissions
    python -m agentos.cli logs     # every delegation and denial
"""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agentos import Kernel  # noqa: E402
from agentos.agents.llm import LLMAgent  # noqa: E402

# What the operator allows. This set is the ceiling for the whole task: the
# planner cannot grant what it does not hold, and neither can anything it
# creates. Add "shell" here and the tree could reach a shell; leave it out and
# no amount of model creativity gets there.
ALLOWED_TOOLS = ["filesystem"]

TASK = "perform an experiment about trees"

# The scripted "reasoning". Each entry is one model turn, in order.
#
# Note what the planner does with events: it invents the name
# "MeasurementsReady", tells the Surveyor to publish it, and tells the Analyst
# to wait for it. Neither worker knows the other exists — the Analyst is woken
# by the runtime when the event fires, which is the p.5 rule holding for
# agents nobody declared. No event name appears anywhere in this file outside
# the planner's own decisions.
PLANNER_SCRIPT = [
    json.dumps({"action": "spawn", "role": "Surveyor",
                "goal": "measure the trees on the plot",
                "tools": ["filesystem"], "model": "surveyor",
                "publishes": ["MeasurementsReady"],
                "subscribes": []}),
    json.dumps({"action": "spawn", "role": "Analyst",
                "goal": "interpret the measurements",
                "tools": [], "model": "analyst",
                "publishes": ["AnalysisReady"],
                "subscribes": ["MeasurementsReady"]}),
    json.dumps({"action": "wait", "events": ["AnalysisReady"]}),
    json.dumps({"action": "done",
                "result": "experiment complete: 3 species sampled, "
                          "growth correlates with canopy gap"}),
]

SURVEYOR_SCRIPT = [
    json.dumps({"action": "tool", "capability": "filesystem", "op": "write",
                "params": {"path": "measurements.txt",
                           "content": "oak 12m\nbirch 8m\npine 15m\n"}}),
    json.dumps({"action": "publish", "event": "MeasurementsReady",
                "payload": {"file": "measurements.txt", "trees": 3}}),
    json.dumps({"action": "done", "result": "3 trees measured"}),
]

ANALYST_SCRIPT = [
    # Reached only after MeasurementsReady arrives: the kernel held this agent
    # until the Surveyor published, without either of them naming the other.
    json.dumps({"action": "publish", "event": "AnalysisReady",
                "payload": {"finding": "growth correlates with canopy gap"}}),
    json.dumps({"action": "done", "result": "measurements interpreted"}),
]

MODELS = {"classes": {
    # Separate scripts so the demo is legible; a real deployment points every
    # class at the same model and lets it decide.
    "fast": [{"provider": "mock", "model": "mock-planner",
              "cost_per_mtok": [1.0, 5.0], "script": PLANNER_SCRIPT}],
    "surveyor": [{"provider": "mock", "model": "mock-surveyor",
                  "cost_per_mtok": [1.0, 5.0], "script": SURVEYOR_SCRIPT}],
    "analyst": [{"provider": "mock", "model": "mock-analyst",
                 "cost_per_mtok": [1.0, 5.0], "script": ANALYST_SCRIPT}],
}}


async def main(slots: int = 4, policy: str = "fifo") -> int:
    from agentos.agents.base import spec_of

    root = Path(__file__).resolve().parent / "_planner_out"
    root.mkdir(exist_ok=True)
    kernel = Kernel(
        slots=slots,
        policy=policy,
        models=MODELS,
        tools={"filesystem": {"root": str(root)}},
        permissions={},  # nothing by name: authority comes from the grant below
    )

    planner = LLMAgent(
        role="Planner",
        goal=TASK,
        tools=ALLOWED_TOOLS,
        model="fast",
        may_spawn=True,
        child_max_steps=4,
    )
    # submit_spec pins the ceiling. Everything below inherits at most this.
    pid = kernel.submit_spec(spec_of(planner), grant=ALLOWED_TOOLS)

    print(f'task: "{TASK}"')
    print(f"operator allows: {', '.join(ALLOWED_TOOLS)}\n")
    await kernel.run()

    proc = kernel.table.get(pid)
    print(f"\nplanner returned: {proc.result}")
    print("\nthe team it invented, and how it wired them:")
    for row in kernel.store.processes():
        print(f"  pid {row['pid']:<3} {row['name']:<10} {row['status']:<9} "
              f"tools={','.join(row['permissions']) or '-':<11} "
              f"publishes={','.join(row['publishes'] or []) or '-':<18} "
              f"waits_for={','.join(row['subscribes'] or []) or '-'}")

    print("\nthe events that actually flowed:")
    for e in kernel.store.events():
        if e["type"] in ("MeasurementsReady", "AnalysisReady"):
            woke = ", ".join(f"pid {p}" for p in e["subscribers"]) or "nobody"
            print(f"  {e['type']:<18} from pid {e['source_pid']} -> woke {woke}")

    print("\nThe Surveyor and the Analyst never named each other. The planner")
    print("invented 'MeasurementsReady', told one to publish it and the other")
    print("to wait for it, and the runtime did the waking — the p.5 rule,")
    print("holding for agents nobody declared. And no tool outside the")
    print("operator's grant was reachable by any of them.")
    kernel.store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
