"""One agent class whose *parameters* are its identity.

Everywhere else in AgentOS an agent is a Python class someone wrote ahead of
time. That is the right shape when the work is known. It is the wrong shape
when a task arrives as a sentence — "perform an experiment about trees" — and
the team that should do it has to be invented on the spot.

`LLMAgent` is the answer to that: a role, a goal, a set of tools, and a model
class. Creating an agent at runtime means constructing those four values,
which are JSON — so a dynamically-invented agent still satisfies the Phase 1
rule that an agent must be re-creatable from its spec. It journals, it
recovers, and it runs in a subprocess like anything else.

The planner is not a special class. It is an `LLMAgent` with `may_spawn`,
which is the only thing separating a team lead from a worker here.

Authority
---------
An agent may delegate only what it holds (`ctx.spawn(..., grant=...)`, checked
in the kernel). So the capability set handed to the root agent is the ceiling
for the entire task tree, however many layers of agents it invents. Give the
planner `["http"]` and nothing it creates, at any depth, can reach the shell.

Protocol
--------
The model is asked to reply with one JSON object per turn:

    {"action": "tool",  "capability": "http", "op": "get", "params": {...}}
    {"action": "spawn", "role": "...", "goal": "...", "tools": [...]}
    {"action": "wait"}
    {"action": "done",  "result": ...}

Anything unparseable is handed back to the model as an observation rather than
raising: a model that returns prose gets a chance to correct itself, and the
run fails only when it runs out of steps.
"""

from __future__ import annotations

import json
from typing import Any

from .base import Agent

MAX_STEPS = 12
MAX_CHILDREN = 8
TRANSCRIPT_CHARS = 6000  # keep the prompt bounded on long runs

SYSTEM = """You are {role}, one agent in a multi-agent runtime.

Your goal: {goal}

Reply with exactly one JSON object and nothing else. Options:
{options}
- {{"action": "done", "result": <your answer>}}

Rules:
- You may only use these tools: {tools}
- Do not explain yourself outside the JSON.
- Prefer finishing over spawning when the goal is within your reach."""

TOOL_OPTION = (
    '- {{"action": "tool", "capability": "<one of your tools>", '
    '"op": "<operation>", "params": {{...}}}}'
)
SPAWN_OPTIONS = (
    '- {{"action": "spawn", "role": "<short name>", "goal": "<what they do>", '
    '"tools": [<subset of your tools>]}}\n'
    '- {{"action": "wait"}}  (block until every agent you spawned has finished)'
)


class ActionError(Exception):
    """The model's reply could not be turned into an action."""


class LLMAgent(Agent):
    """params: role, goal, tools, model, may_spawn, max_steps, max_children."""

    @property
    def name(self) -> str:
        # Every dynamic agent would otherwise be called "LLMAgent" in ps.
        # The role is what a human reading the process table wants to see.
        role = self.params.get("role")
        return str(role) if role else "LLMAgent"

    # -- the loop ------------------------------------------------------------
    async def run(self, ctx: Any) -> Any:
        tools: list[str] = list(self.params.get("tools") or [])
        may_spawn = bool(self.params.get("may_spawn"))
        max_steps = int(self.params.get("max_steps", MAX_STEPS))
        max_children = int(self.params.get("max_children", MAX_CHILDREN))
        model = self.params.get("model", "fast")

        system = SYSTEM.format(
            role=self.params.get("role", "an agent"),
            goal=self.params.get("goal", "(no goal given)"),
            tools=", ".join(tools) or "none",
            options="\n".join(
                ([TOOL_OPTION] if tools else []) + ([SPAWN_OPTIONS] if may_spawn else [])
            ),
        )

        transcript: list[str] = []
        context = self.params.get("context")
        if context:
            transcript.append(f"Context from whoever created you: {context}")
        children: list[int] = []

        for step in range(max_steps):
            reply = await ctx.request_model(
                model, prompt=self._prompt(transcript), system=system
            )
            try:
                action = self._parse(reply["text"])
            except ActionError as exc:
                transcript.append(f"Your last reply was rejected: {exc}. Reply with JSON.")
                continue

            kind = action.get("action")
            if kind == "done":
                return action.get("result")

            if kind == "tool":
                observation = await self._do_tool(ctx, action, tools)
            elif kind == "spawn" and may_spawn:
                observation = await self._do_spawn(
                    ctx, action, tools, children, max_children
                )
            elif kind == "wait" and may_spawn:
                observation = await self._do_wait(ctx, children)
            else:
                observation = (
                    f"Action {kind!r} is not available to you."
                    + ("" if may_spawn else " You cannot spawn agents.")
                )
            transcript.append(observation)

        # Out of steps. Say so rather than pretending: the caller can see how
        # far it got, and the journal has every step.
        return {
            "incomplete": True,
            "reason": f"stopped after {max_steps} steps",
            "transcript": transcript[-3:],
        }

    # -- actions -------------------------------------------------------------
    async def _do_tool(self, ctx, action: dict, tools: list[str]) -> str:
        capability = action.get("capability")
        if capability not in tools:
            return (
                f"Refused: you may not use {capability!r}. "
                f"Your tools are: {', '.join(tools) or 'none'}."
            )
        # Models flatten arguments as often as they nest them. Accept both:
        # {"op": "write", "params": {"path": ...}} and {"op": "write",
        # "path": ...}. Being strict here costs a turn every time and teaches
        # the model nothing the error message could not.
        params = action.get("params")
        if not isinstance(params, dict):
            params = {k: v for k, v in action.items()
                      if k not in ("action", "capability", "op", "params")}
        try:
            value = await ctx.request_tool(
                capability, action.get("op", ""), **params
            )
        except Exception as exc:  # a denial or a tool failure is an observation
            return f"{capability}.{action.get('op')} failed: {exc}"
        return f"{capability}.{action.get('op')} returned: {_clip(value)}"

    async def _do_spawn(
        self, ctx, action: dict, tools: list[str], children: list[int], cap: int
    ) -> str:
        if len(children) >= cap:
            return f"Refused: you have already created {cap} agents, the limit."
        wanted = list(action.get("tools") or [])
        over = [t for t in wanted if t not in tools]
        if over:
            # The kernel would refuse this too; failing here makes the reason
            # legible to the model so it can retry with a smaller set.
            return (
                f"Refused: you cannot grant {', '.join(over)} — you do not hold it. "
                f"You may grant any of: {', '.join(tools) or 'nothing'}."
            )
        child = LLMAgent(
            role=action.get("role", "worker"),
            goal=action.get("goal", ""),
            tools=wanted,
            # Children may be routed to a different capability class than the
            # planner: reasoning to plan, something cheap to execute.
            model=self.params.get("child_model", self.params.get("model", "fast")),
            may_spawn=False,  # one level of delegation per spawn, by default
            max_steps=int(self.params.get("child_max_steps", MAX_STEPS)),
        )
        pid = await ctx.spawn(child, grant=wanted)
        children.append(pid)
        return (
            f"Created agent pid {pid} ({action.get('role')}) with "
            f"{', '.join(wanted) or 'no tools'}. It is running."
        )

    async def _do_wait(self, ctx, children: list[int]) -> str:
        if not children:
            return "You have not created any agents, so there is nothing to wait for."
        results = await ctx.wait_all(agents=list(children))
        return "Your agents finished: " + _clip(results.get("agents"))

    # -- parsing -------------------------------------------------------------
    @staticmethod
    def _prompt(transcript: list[str]) -> str:
        if not transcript:
            return "Begin. What is your first action?"
        body = "\n".join(f"- {line}" for line in transcript)
        return ("What has happened so far:\n" + body[-TRANSCRIPT_CHARS:]
                + "\n\nWhat is your next action?")

    @staticmethod
    def _parse(text: str) -> dict[str, Any]:
        """Find the JSON object in a model reply, tolerating fences and prose."""
        raw = (text or "").strip()
        if raw.startswith("```"):
            raw = raw.strip("`")
            raw = raw[raw.find("\n") + 1:] if "\n" in raw else raw
            if raw.lstrip().startswith("json"):
                raw = raw.lstrip()[4:]
        start, end = raw.find("{"), raw.rfind("}")
        if start == -1 or end <= start:
            raise ActionError("no JSON object found")
        try:
            action = json.loads(raw[start:end + 1])
        except ValueError as exc:
            raise ActionError(f"invalid JSON ({exc})") from exc
        if not isinstance(action, dict) or "action" not in action:
            raise ActionError('JSON must be an object with an "action" key')
        return action


def _clip(value: Any, limit: int = 800) -> str:
    try:
        text = json.dumps(value, default=str)
    except (TypeError, ValueError):
        text = str(value)
    return text if len(text) <= limit else text[:limit] + "… (truncated)"
