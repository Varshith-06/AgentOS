"""The AgentOS side of the workload, importable by the agent's own process."""

from agentos import Agent

from .client import think


class Caller(Agent):
    async def run(self, ctx):
        for i in range(self.params["calls"]):
            think()
            await ctx.request_tool("paidapi", "charge", i=i)
        return "done"
