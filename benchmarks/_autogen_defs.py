"""AutoGen worker for compare.py."""

import json
import sqlite3
import time
from dataclasses import dataclass

from autogen_core import (
    AgentId, MessageContext, RoutedAgent, SingleThreadedAgentRuntime,
    message_handler,
)


@dataclass
class Go:
    n: int
    tally: str
    statefile: str
    delay: float


class Worker(RoutedAgent):
    """Does the billable calls, persisting progress after each one."""

    def __init__(self) -> None:
        super().__init__("worker")
        self.done = 0

    @message_handler
    async def handle(self, message: Go, ctx: MessageContext) -> int:
        for i in range(message.n):
            conn = sqlite3.connect(message.tally, isolation_level=None)
            conn.execute("INSERT INTO calls VALUES (?, ?)", (i, time.time()))
            conn.close()
            if message.delay:
                time.sleep(message.delay)
            self.done = i + 1
            with open(message.statefile, "w", encoding="utf-8") as fh:
                json.dump({"done": self.done}, fh)
        return self.done


async def run(tally: str, statefile: str, n: int, delay: float) -> None:
    runtime = SingleThreadedAgentRuntime()
    await Worker.register(runtime, "worker", lambda: Worker())
    runtime.start()
    await runtime.send_message(
        Go(n=n, tally=tally, statefile=statefile, delay=delay),
        AgentId("worker", "default"),
    )
    await runtime.stop_when_idle()
