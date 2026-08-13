"""One agent class whose *parameters* are its identity."""

from __future__ import annotations

import json
from typing import Any

from .base import Agent

MAX_STEPS = 12
MAX_CHILDREN = 8
TRANSCRIPT_CHARS = 6000

SYSTEM = """You are {role}, one agent in a multi-agent runtime.

Your goal: {goal}

Reply with exactly one JSON object and nothing else. Options:
{options}
- {{"action": "done", "result": <your answer>}}

Rules:
- You may only use these tools: {tools}
{events}- Do not explain yourself outside the JSON.
- Prefer finishing over spawning when the goal is within your reach."""

TOOL_OPTION = (
    '- {{"action": "tool", "capability": "<one of your tools>", '
    '"op": "<operation>", "params": {{...}}}}'
)
SPAWN_OPTIONS = (
    '- {{"action": "spawn", "role": "<short name>", "goal": "<what they do>",\n'
    '     "tools": [<subset of your tools>],\n'
    '     "publishes": [<event names this agent will announce>],\n'
    '     "subscribes": [<event names it should wait for first>],\n'
    '     "priority": "High|Normal|Low", "retries": <0-3, restarts if it crashes>}}\n'
    '- {{"action": "wait"}}  (block until every agent you spawned has finished)\n'
    '- {{"action": "wait", "events": [<event names>]}}  (block until they fire)'
)
PUBLISH_OPTION = (
    '- {{"action": "publish", "event": "<one you may publish>", '
    '"payload": {{...}}}}'
)
MEMORY_OPTIONS = (
    '- {{"action": "remember", "key": "<name>", "value": <any JSON>, '
    '"kind": "shared|private|longterm"}}  (shared: your whole team can recall it)\n'
    '- {{"action": "recall", "key": "<name>"}}  '
    '(or {{"action": "recall", "query": "<text>"}} to search by meaning)'
)
HUMAN_OPTION = (
    '- {{"action": "ask_human", "role": "<who must approve>", '
    '"reason": "<why>"}}  (blocks until a human approves; use before anything '
    'irreversible)'
)
WIRING_NOTE = """- You choose the event names for the agents you create. Pick
  clear ones (e.g. "MeasurementsReady") and use the SAME name in the
  publisher's "publishes" and the waiter's "subscribes" — an agent waiting for
  a name nobody publishes will never wake.
"""
PUBLISH_NOTE = """- You may publish only these events: {allowed}. Publish one
  when the work it names is done, so whoever is waiting on it can continue.
"""


class ActionError(Exception):
    """The model's reply could not be turned into an action."""


class LLMAgent(Agent):
    """params: role, goal, tools, model, may_spawn, max_steps, max_children,
    publishes, subscribes, retries, context.
    """

    @property
    def retries(self) -> int | None:
        """The kernel reads restart budgets off the agent; here it arrives in params."""
        value = self.params.get("retries")
        return value if isinstance(value, int) and value >= 0 else None

    @property
    def name(self) -> str:
        role = self.params.get("role")
        return str(role) if role else "LLMAgent"

    async def run(self, ctx: Any) -> Any:
        tools: list[str] = list(self.params.get("tools") or [])
        may_spawn = bool(self.params.get("may_spawn"))
        max_steps = int(self.params.get("max_steps", MAX_STEPS))
        max_children = int(self.params.get("max_children", MAX_CHILDREN))
        model = self.params.get("model", "fast")
        publishes: list[str] = list(self.params.get("publishes") or [])
        awaits: list[str] = list(self.params.get("subscribes") or [])

        options = ([TOOL_OPTION] if tools else [])
        if publishes:
            options.append(PUBLISH_OPTION)
        if may_spawn:
            options.append(SPAWN_OPTIONS)
        options.append(MEMORY_OPTIONS)
        options.append(HUMAN_OPTION)
        notes = ""
        if may_spawn:
            notes += WIRING_NOTE
        if publishes:
            notes += PUBLISH_NOTE.format(allowed=", ".join(publishes))

        system = SYSTEM.format(
            role=self.params.get("role", "an agent"),
            goal=self.params.get("goal", "(no goal given)"),
            tools=", ".join(tools) or "none",
            events=notes,
            options="\n".join(options),
        )

        transcript: list[str] = []
        context = self.params.get("context")
        if context:
            transcript.append(f"Context from whoever created you: {context}")
        children: list[int] = []

        if awaits:
            arrived = await ctx.wait_all(events=list(awaits))
            transcript.append(
                "The events you were waiting for arrived: "
                + _clip(arrived.get("events"))
            )

        for step in range(max_steps):
            try:
                reply = await ctx.request_model(
                    model, prompt=self._prompt(transcript), system=system
                )
            except Exception as exc:
                return {
                    "incomplete": True,
                    "reason": str(exc),
                    "steps_taken": step,
                    "transcript": transcript[-3:],
                }
            try:
                action = self._parse(reply["text"])
            except ActionError as exc:
                transcript.append(f"Your last reply was rejected: {exc}. Reply with JSON.")
                continue

            kind = action.get("action")
            await ctx.log(f"decided: {_describe(action)}")
            if kind == "done":
                return action.get("result")

            if kind == "tool":
                observation = await self._do_tool(ctx, action, tools)
            elif kind == "publish":
                observation = await self._do_publish(ctx, action, publishes)
            elif kind == "spawn" and may_spawn:
                observation = await self._do_spawn(
                    ctx, action, tools, children, max_children
                )
            elif kind == "wait":
                observation = await self._do_wait(ctx, action, children, may_spawn)
            elif kind == "remember":
                observation = await self._do_remember(ctx, action)
            elif kind == "recall":
                observation = await self._do_recall(ctx, action)
            elif kind == "ask_human":
                observation = await self._do_ask(ctx, action)
            else:
                observation = (
                    f"Action {kind!r} is not available to you."
                    + ("" if may_spawn else " You cannot spawn agents.")
                )
            transcript.append(observation)

        return {
            "incomplete": True,
            "reason": f"stopped after {max_steps} steps",
            "transcript": transcript[-3:],
        }

    async def _do_tool(self, ctx, action: dict, tools: list[str]) -> str:
        capability = action.get("capability")
        if capability not in tools:
            return (
                f"Refused: you may not use {capability!r}. "
                f"Your tools are: {', '.join(tools) or 'none'}."
            )
        # Models flatten arguments as often as they nest them; accept both.
        params = action.get("params")
        if not isinstance(params, dict):
            params = {k: v for k, v in action.items()
                      if k not in ("action", "capability", "op", "params")}
        try:
            value = await ctx.request_tool(
                capability, action.get("op", ""), **params
            )
        except Exception as exc:
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
            return (
                f"Refused: you cannot grant {', '.join(over)} — you do not hold it. "
                f"You may grant any of: {', '.join(tools) or 'nothing'}."
            )
        publishes = [e for e in (action.get("publishes") or []) if isinstance(e, str)]
        subscribes = [e for e in (action.get("subscribes") or []) if isinstance(e, str)]
        child = LLMAgent(
            role=action.get("role", "worker"),
            goal=action.get("goal", ""),
            tools=wanted,
            model=action.get("model")
            or self.params.get("child_model", self.params.get("model", "fast")),
            may_spawn=False,
            max_steps=int(self.params.get("child_max_steps", MAX_STEPS)),
            publishes=publishes,
            subscribes=subscribes,
        )
        priority = action.get("priority")
        if priority in ("High", "Normal", "Low"):
            child.priority = priority
        budget = action.get("retries")
        if isinstance(budget, int) and budget > 0:
            child.params["retries"] = min(budget, 3)
        pid = await ctx.spawn(
            child, grant=wanted, publishes=publishes, subscribes=subscribes
        )
        children.append(pid)
        wiring = ""
        if publishes:
            wiring += f" It will publish: {', '.join(publishes)}."
        if subscribes:
            wiring += f" It waits for: {', '.join(subscribes)} before starting."
        return (
            f"Created agent pid {pid} ({action.get('role')}) with "
            f"{', '.join(wanted) or 'no tools'}.{wiring}"
        )

    async def _do_publish(self, ctx, action: dict, publishes: list[str]) -> str:
        event = action.get("event") or action.get("type")
        if event not in publishes:
            return (
                f"Refused: you were not wired to publish {event!r}. "
                f"You may publish: {', '.join(publishes) or 'nothing'}."
            )
        payload = action.get("payload")
        if not isinstance(payload, dict):
            payload = {k: v for k, v in action.items()
                       if k not in ("action", "event", "type", "payload")}
        await ctx.publish(event, **payload)
        return f"Published {event}. Anyone waiting on it has been woken."

    async def _do_wait(
        self, ctx, action: dict, children: list[int], may_spawn: bool
    ) -> str:
        events = [e for e in (action.get("events") or []) if isinstance(e, str)]
        agents = list(children) if (may_spawn and not events) else []
        if not events and not agents:
            return (
                "There is nothing to wait for: you have created no agents and "
                "named no events."
            )
        try:
            results = await ctx.wait_all(agents=agents, events=events)
        except Exception as exc:
            return f"That wait was refused: {exc}"
        parts = []
        if results.get("agents"):
            parts.append("your agents finished: " + _clip(results["agents"]))
        if results.get("events"):
            parts.append("events arrived: " + _clip(results["events"]))
        return "; ".join(parts) or "The wait resolved."

    async def _do_remember(self, ctx, action: dict) -> str:
        key = action.get("key")
        if not isinstance(key, str) or not key.strip():
            return 'Refused: "remember" needs a non-empty "key".'
        kinds = {"shared": "shared", "private": "working", "longterm": "longterm"}
        kind = kinds.get(action.get("kind", "shared"))
        if kind is None:
            return (
                f"Refused: unknown memory kind {action.get('kind')!r}. "
                f"Use one of: {', '.join(kinds)}."
            )
        try:
            await ctx.memory.store(key, action.get("value"), kind=kind)
        except Exception as exc:
            return f"remember failed: {exc}"
        audience = {
            "shared": "your whole team can recall it",
            "working": "only you can recall it, and it dies with you",
            "longterm": f"future agents named {self.name!r} will see it, and "
                        "text is searchable by meaning with recall+query",
        }[kind]
        return f"Remembered {key!r} ({audience})."

    async def _do_recall(self, ctx, action: dict) -> str:
        query = action.get("query")
        if isinstance(query, str) and query.strip():
            hits = await ctx.memory.retrieve(kind="longterm", query=query)
            return f"Search for {query!r} found: " + _clip(hits, 2000)
        key = action.get("key")
        if not isinstance(key, str) or not key.strip():
            return 'Refused: "recall" needs a "key" or a "query".'
        for kind in ("shared", "working", "longterm"):
            value = await ctx.memory.retrieve(key, kind=kind)
            if value is not None:
                return f"{key!r} = " + _clip(value, 2000)
        return f"Nothing is stored under {key!r} anywhere you can read."

    async def _do_ask(self, ctx, action: dict) -> str:
        role = str(action.get("role") or "Operator")
        reason = str(action.get("reason") or self.params.get("goal", ""))[:300]
        try:
            approval = await ctx.request_approval(role=role, reason=reason)
        except Exception as exc:
            return f"The approval request failed: {exc}"
        by = approval.get("by") if isinstance(approval, dict) else None
        return f"A human ({by or role}) approved: {reason!r}. Proceed."

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


def _describe(action: dict) -> str:
    """One log line per decision, so `agent logs` narrates the model's run."""
    kind = action.get("action")
    detail = {
        "tool": lambda: f"{action.get('capability')}.{action.get('op')}",
        "spawn": lambda: f"{action.get('role')} "
                         f"(tools={','.join(action.get('tools') or []) or '-'})",
        "publish": lambda: action.get("event") or action.get("type"),
        "wait": lambda: ("events " + ",".join(action.get("events") or [])
                         if action.get("events") else "my agents"),
        "remember": lambda: f"{action.get('key')} ({action.get('kind', 'shared')})",
        "recall": lambda: action.get("key") or f"query {action.get('query')!r}",
        "ask_human": lambda: action.get("role") or "Operator",
        "done": lambda: "finishing",
    }.get(kind)
    try:
        return f"{kind}: {detail()}" if detail else str(kind)
    except Exception:
        return str(kind)


def _clip(value: Any, limit: int = 800) -> str:
    try:
        text = json.dumps(value, default=str)
    except (TypeError, ValueError):
        text = str(value)
    return text if len(text) <= limit else text[:limit] + "… (truncated)"
