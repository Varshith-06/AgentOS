"""`agent ps`, `agent top`, `agent logs`, `agent kill`, `agent pause`, `agent resume`.

The CLI is a client, not part of the runtime. It reads the published process
table and pushes control commands back — which is exactly what it will do in
Phase 7 when the transport becomes HTTP instead of SQLite.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import sys
import time
from pathlib import Path

from .kernel.store import Store

STALE_AFTER = 3.0  # seconds without a heartbeat before a runtime looks dead


def _elapsed(row: dict) -> str:
    """Live agents keep ageing between transitions, so the reader does the math."""
    end = row["ended_at"] if row["ended_at"] is not None else time.time()
    return f"{end - row['started_at']:.1f}s"


def _table(rows: list[dict]) -> str:
    if not rows:
        return "no agents (is the runtime running? try: agent run <example>)"
    cols = ["PID", "NAME", "PARENT", "CHILDREN", "STATUS", "PRIORITY", "WAITING ON", "TIME"]
    data = [
        [
            str(r["pid"]),
            r["name"],
            "-" if r["parent"] is None else str(r["parent"]),
            str(r["children"]),
            r["status"],
            r["priority"],
            r["waiting_on"] or "-",
            _elapsed(r),
        ]
        for r in rows
    ]
    widths = [max(len(c), *(len(row[i]) for row in data)) for i, c in enumerate(cols)]
    out = ["  ".join(c.ljust(w) for c, w in zip(cols, widths))]
    out += ["  ".join(v.ljust(w) for v, w in zip(row, widths)) for row in data]
    return "\n".join(out)


def _runtime_banner(store: Store) -> str:
    info = store.runtime_info()
    if not info:
        return "runtime: not started"
    age = time.time() - info["heartbeat"]
    state = "running" if age < STALE_AFTER else f"stale ({int(age)}s since heartbeat)"
    return (
        f"runtime: {state}  os_pid={info['pid_os']}  "
        f"policy={info['policy']}  slots={info['slots']}"
    )


def cmd_ps(args, store: Store) -> int:
    print(_runtime_banner(store))
    print()
    print(_table(store.processes()))
    return 0


def cmd_top(args, store: Store) -> int:
    try:
        while True:
            rows = store.processes()
            live = [r for r in rows if r["status"] not in ("Finished", "Failed")]
            print("\033[2J\033[H", end="")  # clear
            print(_runtime_banner(store))
            print(f"\n{len(live)} live / {len(rows)} total\n")
            print(_table(rows))
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0


def cmd_logs(args, store: Store) -> int:
    for entry in store.logs(pid=args.pid, limit=args.limit):
        ts = time.strftime("%H:%M:%S", time.localtime(entry["ts"]))
        pid = "--" if entry["pid"] is None else str(entry["pid"]).rjust(2)
        print(f"{ts}  pid {pid}  {entry['kind']:<6}  {entry['message']}")
    return 0


def cmd_events(args, store: Store) -> int:
    rows = store.events(limit=args.limit)
    if not rows:
        print("no events yet")
        return 0
    for e in rows:
        ts = time.strftime("%H:%M:%S", time.localtime(e["ts"]))
        src = "kernel" if e["source_pid"] is None else f"pid {e['source_pid']}"
        subs = e["subscribers"]
        woke = (
            "no subscribers"
            if not subs
            else "woke " + ", ".join(f"pid {p}" for p in subs)
        )
        payload = ", ".join(f"{k}={v!r}" for k, v in e["payload"].items())
        print(f"{ts}  {e['type']:<18} from {src:<8} -> {woke}")
        if payload and args.verbose:
            print(f"{'':10}  {payload}")
    return 0


def cmd_control(args, store: Store) -> int:
    store.send_command(args.command, pid=args.pid)
    print(f"{args.command} sent to pid {args.pid}")
    return 0


def cmd_run(args, store: Store) -> int:
    """Run a module that exposes `main(kernel)` or an `App` agent."""
    path = Path(args.target)
    if path.exists():
        sys.path.insert(0, str(path.parent.resolve()))
        module = importlib.import_module(path.stem)
    else:
        module = importlib.import_module(args.target)
    if not hasattr(module, "main"):
        print(f"{args.target} has no main(kernel) function", file=sys.stderr)
        return 1
    return asyncio.run(module.main(slots=args.slots, policy=args.policy))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="agent", description="AgentOS control CLI")
    p.add_argument("--dir", default=".agentos", help="runtime state directory")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("ps", help="list agent processes")

    top = sub.add_parser("top", help="live process table")
    top.add_argument("--interval", type=float, default=0.5)

    logs = sub.add_parser("logs", help="show the kernel log")
    logs.add_argument("pid", nargs="?", type=int, default=None)
    logs.add_argument("--limit", type=int, default=200)

    events = sub.add_parser("events", help="show the event timeline (p.5)")
    events.add_argument("--limit", type=int, default=200)
    events.add_argument("-v", "--verbose", action="store_true", help="show payloads")

    for name, help_text in [
        ("kill", "terminate an agent and its children"),
        ("pause", "suspend an agent at its next syscall"),
        ("resume", "resume a suspended agent"),
    ]:
        c = sub.add_parser(name, help=help_text)
        c.add_argument("pid", type=int)

    run = sub.add_parser("run", help="run an example or app module")
    run.add_argument("target")
    run.add_argument("--slots", type=int, default=4)
    run.add_argument("--policy", default="fifo")

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    store = Store(args.dir)
    handlers = {
        "ps": cmd_ps,
        "top": cmd_top,
        "logs": cmd_logs,
        "events": cmd_events,
        "run": cmd_run,
        "kill": cmd_control,
        "pause": cmd_control,
        "resume": cmd_control,
    }
    try:
        return handlers[args.command](args, store)
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
