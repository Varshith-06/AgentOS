"""The AgentOS side of the workload, importable by the agent's own process."""

import os
from pathlib import Path

from agentos import Agent

from .client import (check, mark_model, model_path, model_prompt, model_reply, question, task,
                     task_prompt, think)


class Caller(Agent):
    async def run(self, ctx):
        # Every life says where it lives, so an agent crash can kill this
        # process and leave the kernel be.
        if os.environ.get("CS_DIR"):
            Path(os.environ["CS_DIR"], "agent.pid").write_text(str(os.getpid()))
        base = self.params.get("base", 0)
        notes: list[str] = []  # CS_TASK: rebuilt from the journal in each life
        for i in range(base, base + self.params["calls"]):
            think()
            q = task_prompt(i, notes) if task() else question(i)
            if model_path():
                mark_model("send", i)
                reply = await ctx.request_model("fast", model_prompt(i, q), max_tokens=64)
                mark_model("reply", i)
                out = model_reply(i, reply["text"])
            else:
                out = await ctx.request_tool("paidapi", "charge", i=i, q=q)
            check(i, q, out)
            notes.append(out.get("text") or f"S{i}: noted.")
        return "done"
