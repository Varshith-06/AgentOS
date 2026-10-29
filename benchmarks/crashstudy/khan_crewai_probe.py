"""Khan's CrewAI CheckpointConfig case: s1 completes, s2 raises, restore the
newest checkpoint with Flow.from_checkpoint and kickoff again. Counts how many
times s1 ran. Exactly-once restore gives s1=1."""
import json, sys, tempfile
from pathlib import Path

import crewai
from crewai import CheckpointConfig
from crewai.flow.flow import Flow, listen, start
from pydantic import BaseModel

where = Path(tempfile.mkdtemp()) / "ck"
count = {"s1": 0, "s2": 0}
fail = {"on": True}


class S(BaseModel):
    id: str = "khan-probe"
    n: int = 0


class F(Flow[S]):
    @start()
    def s1(self):
        count["s1"] += 1
        self.state.n += 1

    @listen(s1)
    def s2(self):
        count["s2"] += 1
        if fail["on"]:
            raise RuntimeError("crash in s2")


config = dict(location=str(where), on_events=["method_execution_finished"])
try:
    F(checkpoint=CheckpointConfig(**config)).kickoff()
except Exception as e:
    print("first run raised:", type(e).__name__)
saved = sorted(where.rglob("*.json"), key=lambda p: p.stat().st_mtime)
print("checkpoints:", len(saved))
fail["on"] = False
F.from_checkpoint(CheckpointConfig(**config, restore_from=str(saved[-1]))).kickoff()
print(json.dumps({"crewai": crewai.__version__, **count}))
