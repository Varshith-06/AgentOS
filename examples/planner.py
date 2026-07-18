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
PLANNER_SCRIPT = [
    json.dumps({"action": "spawn", "role": "Surveyor",
                "goal": "measure the trees on the plot",
                "tools": ["filesystem"]}),
    json.dumps({"action": "spawn", "role": "Analyst",
                "goal": "interpret the measurements",
                "tools": []}),
    json.dumps({"action": "wait"}),
    json.dumps({"action": "done",
                "result": "experiment complete: 3 species sampled, "
                          "growth correlates with canopy gap"}),
]

WORKER_SCRIPT = [
    json.dumps({"action": "tool", "capability": "filesystem", "op": "write",
                "params": {"path": "measurements.txt",
                           "content": "oak 12m\nbirch 8m\npine 15m\n"}}),
    json.dumps({"action": "done", "result": "3 trees measured"}),
]

MODELS = {"classes": {
    # The planner and the workers get different scripts so the demo is
    # legible; a real deployment points both at the same model.
    "fast": [{"provider": "mock", "model": "mock-planner",
              "cost_per_mtok": [1.0, 5.0], "script": PLANNER_SCRIPT}],
    "worker": [{"provider": "mock", "model": "mock-worker",
                "cost_per_mtok": [1.0, 5.0], "script": WORKER_SCRIPT}],
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
        child_model="worker",  # planners reason; workers execute
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
    print("\nthe team it invented:")
    for row in kernel.store.processes():
        print(f"  pid {row['pid']:<3} {row['name']:<12} {row['status']:<9} "
              f"tools={','.join(row['permissions']) or '-'}")
    print("\nnobody could reach a tool the operator did not allow — "
          "the grant is the ceiling.")
    kernel.store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
