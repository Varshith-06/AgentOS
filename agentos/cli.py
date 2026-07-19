"""`agent ps / top / wait / logs / events / approvals / tools / kill / pause /
resume / approve / grant / revoke`.

The CLI is a client, not part of the runtime. It reads the published process
table and pushes control commands back — which is exactly what it will do in
Phase 7 when the transport becomes HTTP instead of SQLite.

`approve`, `grant`, and `revoke` are the exception to the command queue: they
are durable state, written straight to the store or the permissions file. That
is what lets a human approve — or revoke a capability from — a runtime that is
not even running; a live runtime picks the change up within a tick.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import sys
import time
from pathlib import Path

from .drivers import REGISTRY
from .kernel.permissions import Permissions
from .kernel.store import Store

STALE_AFTER = 3.0  # seconds without a heartbeat before a runtime looks dead


def _elapsed(row: dict) -> str:
    """Live agents keep ageing between transitions, so the reader does the math."""
    end = row["ended_at"] if row["ended_at"] is not None else time.time()
    return f"{end - row['started_at']:.1f}s"


def _bytes_str(n: int) -> str:
    if n <= 0:
        return "-"
    if n < 1024:
        return f"{n}B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f}K"
    return f"{n / (1024 * 1024):.1f}M"


def _table(rows: list[dict], store: Store | None = None) -> str:
    if not rows:
        return "no agents (is the runtime running? try: agent run <example>)"
    mem = store.memory_usage() if store else {}
    costs = store.model_costs() if store else {}
    # The p.3 process card, one row per agent: MODEL and PERMS are on that
    # card next to PID and status, so they belong here and not only in
    # `agent tools`.
    cols = [
        "PID", "NAME", "PARENT", "CHILDREN", "STATUS", "PRIORITY",
        "WAITING ON", "MODEL", "PERMS", "EVENTS", "MEM", "COST", "CKPT", "TIME",
    ]
    data = [
        [
            str(r["pid"]),
            r["name"],
            "-" if r["parent"] is None else str(r["parent"]),
            str(r["children"]),
            r["status"] + (f" x{r['retries']}" if r.get("retries") else ""),
            r["priority"],
            r["waiting_on"] or "-",
            r.get("model") or "-",
            ",".join(r.get("permissions") or []) or "-",
            # The wiring a parent gave a runtime-invented agent (p.5): what it
            # may announce, and what it waits for. "-" for unwired agents.
            " ".join(
                part for part in (
                    "pub:" + ",".join(r["publishes"]) if r.get("publishes") else "",
                    "sub:" + ",".join(r["subscribes"]) if r.get("subscribes") else "",
                ) if part
            ) or "-",
            # private memory follows the pid; longterm/semantic follow the name
            _bytes_str(mem.get(str(r["pid"]), 0) + mem.get(r["name"], 0)),
            f"${costs[r['pid']]['cost']:.4f}" if r["pid"] in costs else "-",
            f"#{r['checkpoint']}" if r.get("checkpoint") else "-",
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
    print(_table(store.processes(), store))
    return 0


def cmd_wait(args, store: Store) -> int:
    """`agent wait <pid>` (p.8) — block until an agent terminates.

    A polling reader, not a kernel call: the CLI is a client and the process
    table is the contract between them. Exits 0 if the agent finished, 1 if
    it failed, 2 if the runtime went away while we were waiting.
    """
    deadline = time.time() + args.timeout if args.timeout else None
    last = None
    while True:
        row = next((r for r in store.processes() if r["pid"] == args.pid), None)
        if row is None:
            print(f"no such agent: pid {args.pid}")
            return 2
        if row["status"] != last:
            last = row["status"]
            if not args.quiet:
                print(f"pid {args.pid} {row['name']}: {last}"
                      + (f" (waiting on {row['waiting_on']})" if row["waiting_on"] else ""))
        if row["status"] in ("Finished", "Failed"):
            if row["status"] == "Finished":
                print(f"result: {row['result']!r}")
                return 0
            print(f"failed: {row['exit_reason']}")
            return 1
        info = store.runtime_info()
        if info and time.time() - info["heartbeat"] > STALE_AFTER:
            print(f"runtime went stale while waiting on pid {args.pid}")
            return 2
        if deadline and time.time() > deadline:
            print(f"timed out after {args.timeout}s; pid {args.pid} is {last}")
            return 2
        time.sleep(0.1)


def cmd_top(args, store: Store) -> int:
    try:
        while True:
            rows = store.processes()
            live = [r for r in rows if r["status"] not in ("Finished", "Failed")]
            print("\033[2J\033[H", end="")  # clear
            print(_runtime_banner(store))
            print(f"\n{len(live)} live / {len(rows)} total\n")
            print(_table(rows, store))
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


def cmd_approve(args, store: Store) -> int:
    try:
        row = store.approve(args.pid, args.role)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1
    print(f"pid {args.pid} ({row['agent']}) approved as {args.role}: {row['reason']}")
    info = store.runtime_info()
    if not info or time.time() - info["heartbeat"] > STALE_AFTER:
        print("runtime is not running — the grant is durable and honored on restart")
    return 0


def cmd_approvals(args, store: Store) -> int:
    rows = store.approvals(include_consumed=args.all)
    if not rows:
        print("no approvals" + ("" if args.all else " outstanding"))
        return 0
    cols = ["ID", "PID", "AGENT", "ROLE", "REASON", "STATUS", "REQUESTED"]
    data = [
        [
            str(r["id"]),
            "-" if r["pid"] is None else str(r["pid"]),
            r["agent"],
            r["role"],
            r["reason"],
            r["status"],
            time.strftime("%H:%M:%S", time.localtime(r["requested_at"])),
        ]
        for r in rows
    ]
    widths = [max(len(c), *(len(row[i]) for row in data)) for i, c in enumerate(cols)]
    print("  ".join(c.ljust(w) for c, w in zip(cols, widths)))
    for row in data:
        print("  ".join(v.ljust(w) for v, w in zip(row, widths)))
    return 0


def _perms(args) -> Permissions:
    return Permissions(path=Path(args.dir) / "permissions.json")


def cmd_grant(args, store: Store) -> int:
    perms = _perms(args)
    perms.grant(args.agent, args.capability)
    print(f"granted {args.capability!r} to {args.agent}")
    return 0


def cmd_revoke(args, store: Store) -> int:
    perms = _perms(args)
    if not perms.allowed(args.agent, args.capability):
        print(f"{args.agent} did not hold {args.capability!r} anyway")
        return 0
    perms.revoke(args.agent, args.capability)
    print(f"revoked {args.capability!r} from {args.agent}")
    info = store.runtime_info()
    if info and time.time() - info["heartbeat"] < STALE_AFTER:
        print("a running runtime picks this up within a tick")
    return 0


def cmd_tools(args, store: Store) -> int:
    perms = _perms(args)
    print("CAPABILITY    OPS")
    for name, cls in sorted(REGISTRY.items()):
        ops = ", ".join(sorted(m[3:] for m in dir(cls) if m.startswith("op_")))
        print(f"{name:<12}  {ops}")
    if perms.grants:
        print("\nAGENT         CAPABILITIES")
        for agent, caps in sorted(perms.grants.items()):
            print(f"{agent:<12}  {', '.join(sorted(caps))}")
    else:
        print("\nno capabilities granted (deny by default)")
    return 0


def cmd_daemon(args, store: Store) -> int:
    """Run the shared runtime that outlives any application (Phase 7, p.8)."""
    info = store.runtime_info()
    if info and time.time() - info["heartbeat"] < STALE_AFTER:
        print("a runtime is already running here", file=sys.stderr)
        return 1

    from .runtime.daemon import Daemon

    rows = store.processes()
    recover = args.recover and any(
        r["status"] not in ("Finished", "Failed") for r in rows
    )
    daemon = Daemon(
        store=store,
        host=args.host,
        port=args.port,
        policy=args.policy,
        slots=args.slots,
        transport=args.transport,
        task_tools=[t.strip() for t in args.task_tools.split(",") if t.strip()],
        task_budget_usd=args.task_budget,
        token=args.token or None,
        insecure=args.insecure,
        recover=recover,
    )
    print(f"agentos daemon at {daemon.url}  "
          f"(policy={args.policy}, slots={args.slots}, "
          f"transport={args.transport})")
    print(f"dashboard: {daemon.url}/")
    if recover:
        print("recovering the previous run's agents from their journals")
    print("applications connect with agentos.client.RuntimeClient; Ctrl-C stops it")
    try:
        asyncio.run(daemon.start())
    except KeyboardInterrupt:
        print("\ndaemon stopped")
    return 0


def cmd_recover(args, store: Store) -> int:
    """Resume agents from their journals after a crash (Phase 6, p.7-8)."""
    info = store.runtime_info()
    if info and time.time() - info["heartbeat"] < STALE_AFTER:
        print("a runtime is still running; refusing to recover over it", file=sys.stderr)
        return 1
    rows = store.processes()
    alive = [r for r in rows if r["status"] not in ("Finished", "Failed")]
    if not alive:
        print("nothing to recover" + (" - every agent already terminal" if rows else ""))
        return 0

    policy = args.policy or (info["policy"] if info else "fifo")
    slots = args.slots or (info["slots"] if info else 4)
    names = ", ".join(f"pid {r['pid']} ({r['name']})" for r in alive)
    print(f"recovering {len(alive)} agent(s): {names}")

    from .kernel.kernel import Kernel

    kernel = Kernel(policy=policy, slots=slots, store=store, recover=True)
    asyncio.run(kernel.run())

    print()
    for row in store.processes():
        print(f"  pid {row['pid']:>2}  {row['name']:<16} {row['status']:<9} {row['exit_reason'] or ''}")
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

    wait = sub.add_parser("wait", help="block until an agent terminates (p.8)")
    wait.add_argument("pid", type=int)
    wait.add_argument("--timeout", type=float, default=0.0,
                      help="give up after N seconds (0 = wait forever)")
    wait.add_argument("-q", "--quiet", action="store_true",
                      help="only print the final result")

    approve = sub.add_parser("approve", help="grant a pending human approval (p.6)")
    approve.add_argument("pid", type=int)
    approve.add_argument(
        "--as",
        dest="role",
        required=True,
        help='the approving role, e.g. --as "Senior Engineer"',
    )

    approvals = sub.add_parser("approvals", help="list human approvals")
    approvals.add_argument(
        "--all", action="store_true", help="include consumed approvals"
    )

    for name, help_text in [
        ("grant", "grant a capability to an agent name (p.7)"),
        ("revoke", "revoke a capability; applies to a running system"),
    ]:
        c = sub.add_parser(name, help=help_text)
        c.add_argument("agent", help="agent name, or * for every agent")
        c.add_argument("capability", help="e.g. sql, filesystem, or *")

    sub.add_parser("tools", help="list tool drivers and the permission matrix")

    run = sub.add_parser("run", help="run an example or app module")
    run.add_argument("target")
    run.add_argument("--slots", type=int, default=4)
    run.add_argument("--policy", default="fifo")

    recover = sub.add_parser(
        "recover", help="resume agents from their journals after a crash (p.7-8)"
    )
    recover.add_argument("--slots", type=int, default=None)
    recover.add_argument("--policy", default=None)

    daemon = sub.add_parser(
        "daemon", help="run the shared runtime that outlives applications (p.8)"
    )
    daemon.add_argument("--host", default="127.0.0.1")
    daemon.add_argument("--port", type=int, default=7070)
    daemon.add_argument("--slots", type=int, default=4)
    daemon.add_argument("--policy", default="fifo")
    daemon.add_argument(
        "--token", default="",
        help="bearer token every API request must present. Falls back to "
             "$AGENTOS_TOKEN. Required to serve any address but loopback",
    )
    daemon.add_argument(
        "--insecure", action="store_true",
        help="serve a non-loopback address with no token. Only when something "
             "in front of this already authenticates",
    )
    daemon.add_argument(
        "--task-budget", type=float, default=None, metavar="USD",
        help="most a submitted task may spend on models, e.g. 0.50. A request "
             "may ask for less. Unset means submitted tasks are unmetered",
    )
    daemon.add_argument(
        "--task-tools", default="",
        help="comma-separated capabilities POST /task may grant, e.g. "
             "'filesystem,http'. Empty (the default) means submitted tasks "
             "get no tools; this is the ceiling for everything they create",
    )
    daemon.add_argument(
        "--transport", choices=["socket", "pipe"], default="socket",
        help="syscall channel to subprocess agents: loopback TCP (default) "
             "or stdio pipes; ignored with --isolation task",
    )
    daemon.add_argument(
        "--recover", action="store_true",
        help="resume the previous run's agents from their journals first",
    )

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    store = Store(args.dir)
    handlers = {
        "ps": cmd_ps,
        "top": cmd_top,
        "wait": cmd_wait,
        "logs": cmd_logs,
        "events": cmd_events,
        "run": cmd_run,
        "kill": cmd_control,
        "pause": cmd_control,
        "resume": cmd_control,
        "approve": cmd_approve,
        "approvals": cmd_approvals,
        "grant": cmd_grant,
        "revoke": cmd_revoke,
        "tools": cmd_tools,
        "recover": cmd_recover,
        "daemon": cmd_daemon,
    }
    try:
        return handlers[args.command](args, store)
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
