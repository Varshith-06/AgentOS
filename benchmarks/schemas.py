"""Does putting each op's params in the prompt cut malformed tool calls?

The same tasks run twice per model: once with the prompt naming capabilities
only (how agents were prompted before), once with each op's JSON Schema
(native ops read off their signatures, MCP tools from tools/list). A call is
malformed if it names an op that does not exist, passes arguments the op does
not take, or misses one it needs. Two MCP servers sit beside the native tools,
because an MCP server's tool names cannot be guessed from its capability name.

    GOOGLE_API_KEY=... python benchmarks/schemas.py --model gemini --reps 3
    GROQ_API_KEY=...   python benchmarks/schemas.py --model allam  --reps 3
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agentos import Kernel  # noqa: E402
from agentos.agents.base import spec_of  # noqa: E402
from agentos.agents.llm import LLMAgent  # noqa: E402
from agentos.kernel.store import Store  # noqa: E402

SERVER = str(Path(__file__).resolve().parents[1] / "tests" / "fake_mcp_server.py")
MODELS = {
    "gemini": {"provider": "openai", "model": "gemini-3.5-flash-lite",
               "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
               "api_key_env": "GOOGLE_API_KEY", "cost_per_mtok": [0, 0]},
    "allam": {"provider": "openai", "model": "allam-2-7b",
              "base_url": "https://api.groq.com/openai/v1",
              "api_key_env": "GROQ_API_KEY", "cost_per_mtok": [0, 0]},
}


def _tool(name, description, props, required=None):
    return {"name": name, "description": description,
            "inputSchema": {"type": "object", "properties": props,
                            "required": required if required is not None else sorted(props),
                            "additionalProperties": False}}


NOTES = [
    _tool("create_note", "Save a note.",
          {"title": {"type": "string"}, "body": {"type": "string"},
           "tags": {"type": "array", "items": {"type": "string"}}}, ["title", "body"]),
    _tool("search_notes", "Find saved notes containing some text.",
          {"text": {"type": "string"}, "limit": {"type": "integer"}}, ["text"]),
]
CALC = [_tool("add_numbers", "Add two numbers.",
              {"x": {"type": "number"}, "y": {"type": "number"}})]
SAVED = [{"title": "Groceries", "body": "eggs, milk"}, {"title": "Todo", "body": "buy milk"},
         {"title": "Ideas", "body": "a bike shed"}]


def _log(env):
    path = env["log"]
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def _says(result, number: int) -> bool:
    """A finished answer that states this number (not "429" from an error)."""
    if isinstance(result, dict) and result.get("incomplete"):
        return False
    text = result if isinstance(result, str) else json.dumps(result, default=str)
    return re.search(rf"(?<![\d.]){number}(?![\d])", text) is not None


# Tasks scored by the answer alone, so a stored result can be scored again.
ANSWERS = {1: 4817, 3: 12, 5: 2, 6: 42}


def _read(env, name):
    path = env["root"] / name
    return path.read_text().strip() if path.exists() else None


def _rows(env, query):
    try:
        conn = sqlite3.connect(env["db"])
        try:
            return conn.execute(query).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return None


# goal, tools, check(env, result) -> did the task get done
TASKS = [
    ("Create a file called report.txt whose whole content is the word done.",
     ["filesystem"], lambda env, r: _read(env, "report.txt") == "done"),
    ("Read data.txt and give the number written in it as your result.",
     ["filesystem"], lambda env, r: _says(r, 4817)),
    ("In the database, create a table fruits with columns name and qty, insert apples "
     "with qty 3 and pears with qty 5, and give the total qty as your result.",
     ["sql"], lambda env, r: _rows(env, "SELECT SUM(qty) FROM fruits") == [(8,)]),
    ("Give the number of rows in the database table orders as your result.",
     ["sql"], lambda env, r: _says(r, 12)),
    ("Save a note titled Groceries whose body is 'eggs, milk', tagged home.",
     ["mcp:notes"], lambda env, r: any(
         c["tool"] == "create_note" and c["args"].get("title") == "Groceries"
         and "home" in (c["args"].get("tags") or []) for c in _log(env))),
    ("Search the saved notes for milk and give how many matched as your result.",
     ["mcp:notes"], lambda env, r: _says(r, 2)),
    ("Add 17 and 25 with the calculator and give the sum as your result.",
     ["mcp:calc"], lambda env, r: _says(r, 42)),
    ("Read numbers.txt, which holds two numbers on separate lines, add them with the "
     "calculator, and write the sum to sum.txt.",
     ["filesystem", "mcp:calc"], lambda env, r: _read(env, "sum.txt") == "1000"),
]

MALFORMED = re.compile(
    r"has no op|has no tool|no tool '|bad arguments|unexpected keyword|missing \d* ?"
    r"required|invalid arguments|takes \d+ positional|needs a capability name"
    r"|no driver for capability", re.I)


def _env(tmp: Path) -> dict:
    root = tmp / "fs"
    root.mkdir()
    (root / "data.txt").write_text("4817\n")
    (root / "numbers.txt").write_text("123\n877\n")
    db = tmp / "shop.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE orders (id INTEGER)")
    conn.executemany("INSERT INTO orders VALUES (?)", [(i,) for i in range(12)])
    conn.commit()
    conn.close()
    log = tmp / "mcp.jsonl"
    servers = {}
    for name, tools, extra in (("notes", NOTES, {"notes": SAVED}), ("calc", CALC, {})):
        cfg = tmp / f"{name}.json"
        cfg.write_text(json.dumps({"name": name, "tools": tools, "log": str(log), **extra}))
        servers[f"mcp:{name}"] = {"command": [sys.executable, SERVER, str(cfg)]}
    return {"root": root, "db": db, "log": log, "servers": servers}


async def run_task(goal, tools, check, model, schemas: bool) -> dict:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_name:
        tmp = Path(tmp_name)
        env = _env(tmp)
        store = Store(tmp / "rt")
        kernel = Kernel(
            store=store, tick=0.01, permissions={},
            tools={"filesystem": {"root": str(env["root"])}, "sql": {"db": str(env["db"])},
                   **env["servers"]},
            models={"classes": {"fast": [model]}},
        )
        agent = LLMAgent(role="Worker", goal=goal, tools=tools, schemas=schemas,
                         may_spawn=False, max_steps=8)
        pid = kernel.submit_spec(spec_of(agent), grant=tools)
        try:
            await asyncio.wait_for(kernel.run(), timeout=300)
        except asyncio.TimeoutError:
            # The kernel's own cleanup never ran: end the agents and servers here,
            # before the store they write to is closed.
            tasks = [p.task for p in kernel.table.all() if p.task and not p.task.done()]
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            for d in kernel._drivers.values():
                if hasattr(d, "close"):
                    await d.close()
        result = kernel.table.get(pid).result
        decided = [row["message"].split("decided: tool: ", 1)[1] for row in store.logs(limit=5000)
                   if row["kind"] == "agent" and row["message"].startswith("decided: tool: ")]
        calls = store.db.execute("SELECT capability, op, ok, error FROM tool_calls").fetchall()
        usage = store.model_usage()
        answered = sum(u["calls"] - u["failed"] for u in usage.values())
        failed_model = sum(u["failed"] for u in usage.values())
        store.close()
        done = bool(check(env, result))
    reached = len(calls)
    malformed = sum(1 for c in calls if not c["ok"] and MALFORMED.search(c["error"] or ""))
    # A tool action the agent refused before it reached the kernel names a
    # capability the agent does not hold: malformed too.
    unheld = max(0, len(decided) - reached)
    return {"goal": goal[:40], "tools": tools, "schemas": schemas, "done": done,
            "actions": len(decided), "malformed": malformed + unheld,
            "other_errors": sum(1 for c in calls if not c["ok"]) - malformed,
            "answered": answered, "model_failures": failed_model,
            "result": json.dumps(result, default=str)[:200]}


async def main(model_name: str, reps: int, gap: float, out: Path) -> int:
    model = MODELS[model_name]
    rows = []
    for rep in range(reps):
        for i, (goal, tools, check) in enumerate(TASKS):
            for schemas in (False, True):
                r = await run_task(goal, tools, check, model, schemas)
                r.update(model=model["model"], rep=rep, task=i)
                rows.append(r)
                with open(out, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(r) + "\n")
                print(f"rep {rep} task {i} schemas={schemas!s:5} done={r['done']!s:5} "
                      f"actions={r['actions']} malformed={r['malformed']} "
                      f"model_failures={r['model_failures']}", flush=True)
                time.sleep(gap)
    summarize(rows)
    return 0


def _is_json(text: str) -> bool:
    try:
        json.loads(text)
        return True
    except ValueError:
        return False


def summarize(rows: list[dict]) -> None:
    # A run whose model call failed (a free tier's rate limit, mostly) ended
    # for a reason that has nothing to do with the prompt.
    kept = [r for r in rows if r["answered"] and not r["model_failures"]]
    for r in kept:
        if r["task"] in ANSWERS:  # scored again, by the stricter check
            r["done"] = _says(json.loads(r["result"]) if _is_json(r["result"]) else r["result"],
                              ANSWERS[r["task"]])
    print(f"\n{len(rows) - len(kept)} of {len(rows)} runs left out: a model call failed in them")
    rows = kept
    print(f"\n{'prompt':<22} {'runs':>5} {'tool actions':>13} {'malformed':>10} "
          f"{'rate':>6} {'tasks done':>11}")
    for schemas in (False, True):
        sel = [r for r in rows if r["schemas"] is schemas]
        acts = sum(r["actions"] for r in sel)
        bad = sum(r["malformed"] for r in sel)
        done = sum(r["done"] for r in sel)
        label = "with schemas" if schemas else "capability names only"
        print(f"{label:<22} {len(sel):>5} {acts:>13} {bad:>10} "
              f"{bad / acts if acts else 0:>6.0%} {done:>5}/{len(sel)}")
    for cap_kind, pred in (("native tasks", lambda r: not any(t.startswith("mcp:") for t in r["tools"])),
                           ("MCP tasks", lambda r: any(t.startswith("mcp:") for t in r["tools"]))):
        print(f"\n  {cap_kind}:")
        for schemas in (False, True):
            sel = [r for r in rows if r["schemas"] is schemas and pred(r)]
            acts = sum(r["actions"] for r in sel)
            bad = sum(r["malformed"] for r in sel)
            print(f"    {'with schemas' if schemas else 'names only':<14} "
                  f"{bad}/{acts} malformed, {sum(r['done'] for r in sel)}/{len(sel)} done")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", choices=sorted(MODELS), default="gemini")
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--gap", type=float, default=4.0, help="seconds between runs")
    ap.add_argument("--out", default=None)
    ap.add_argument("--summary", help="only summarize an existing results file")
    a = ap.parse_args()
    if a.summary:
        summarize([json.loads(line) for line in Path(a.summary).read_text().splitlines()])
        raise SystemExit(0)
    out = Path(a.out or Path(__file__).parent / "results" / f"schemas-{a.model}.jsonl")
    out.parent.mkdir(parents=True, exist_ok=True)
    raise SystemExit(asyncio.run(main(a.model, a.reps, a.gap, out)))
