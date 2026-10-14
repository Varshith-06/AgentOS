"""AutoGen worker for the crash study: progress saved with save_state after each call."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from autogen_core import MessageContext, RoutedAgent, message_handler

from .client import charge, keyed, think

STATE = Path(os.environ.get("CS_DIR", ".")) / "autogen.json"
RUN = os.environ.get("CS_RUN", "run")
RUNTIME = None


@dataclass
class Go:
    n: int


class Worker(RoutedAgent):
    def __init__(self) -> None:
        super().__init__("worker")
        self.done = 0

    async def save_state(self):
        return {"done": self.done}

    async def load_state(self, state):
        self.done = state["done"]

    @message_handler
    async def handle(self, message: Go, ctx: MessageContext) -> int:
        while self.done < message.n:
            think()
            charge(self.done, f"{RUN}:ag:{self.done}" if keyed() else None)
            self.done += 1
            STATE.write_text(json.dumps(await RUNTIME.save_state()))
        return self.done
