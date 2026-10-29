"""The AgentOS side of the workload, importable by the agent's own process."""

import os
from pathlib import Path

from agentos import Agent

from .client import think


class Caller(Agent):
    async def run(self, ctx):
        # Every life says where it lives, so an agent crash can kill this
        # process and leave the kernel be.
        if os.environ.get("CS_DIR"):
            Path(os.environ["CS_DIR"], "agent.pid").write_text(str(os.getpid()))
        for i in range(self.params["calls"]):
            think()
            await ctx.request_tool("paidapi", "charge", i=i)
        return "done"
