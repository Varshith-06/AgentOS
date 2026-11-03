"""A crash of the host itself: the whole Linux VM goes, couriers and all.

    python -m benchmarks.crashstudy.hostcrash --courier on --trials 20 --out results/v2/host.jsonl

AgentOS runs inside WSL 2 and the stand-in provider outside it, on Windows, so
the bill survives what kills the runtime. The kill is `wsl --shutdown`, which
stops the VM and everything in it at once; then AgentOS recovers in a new VM.
A courier cannot survive this, so a call in flight is lost to recovery. What is
under test is whether that repeat is at least flagged, and whether the store
holds up: `--durable` fsyncs every commit, the default leaves that to SQLite.

It is a stopped VM rather than pulled power: writes the VM had already passed
to its virtual disk are not lost, so this understates what power loss can do.
"""

from __future__ import annotations

import argparse
import json
import random
import shlex
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from benchmarks.crashstudy import paidapi  # noqa: E402
from benchmarks.crashstudy.run import charge_times, drain  # noqa: E402

DISTRO = "Ubuntu"


def wsl(script: str, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(["wsl", "-d", DISTRO, "--exec", "bash", "-lc", script],
                          capture_output=True, text=True, **kw)


def host_ip() -> str:
    """Windows as the VM sees it. It can change when the VM restarts."""
    out = wsl("ip route show default | awk '{print $3}'", timeout=120).stdout.strip()
    if not out:
        raise RuntimeError("no default route in the VM")
    return out.splitlines()[-1]


def command(phase: str, run: str, port: int, args, ip: str) -> str:
    env = {"PYTHONPATH": ".", "PYTHONUTF8": "1", "CS_RUN": run, "CS_HOST": ip,
           "CS_PORT": str(port), "CS_DIR": f"$HOME/hc/{run}", "CS_CALLS": str(args.calls),
           "CS_THINK": str(args.think), "CS_KEYED": "0", "CS_ADOPT": "1",
           "CS_COURIER": "1" if args.courier == "on" else "0", "CS_RETRIES": "0"}
    if args.durable:
        env["CS_DURABLE"] = "1"
    pairs = " ".join(f'{k}="{v}"' for k, v in env.items())
    return (f"mkdir -p ~/hc/{run} && cd ~/aos && {pairs} python3 -m "
            f"benchmarks.crashstudy.adapters agentos {phase}")


READ = r"""
import json, os, sqlite3, sys
d = os.path.expanduser(sys.argv[1])
db = os.path.join(d, "agentos", "runtime.db")
out = {"store": os.path.exists(db)}
if out["store"]:
    c = sqlite3.connect(db)
    out["announced"] = c.execute("SELECT COUNT(*) FROM events WHERE type='OutcomeAmbiguous'").fetchone()[0]
    out["processes"] = c.execute("SELECT COUNT(*) FROM processes").fetchone()[0]
    out["journal"] = c.execute("SELECT COUNT(*) FROM journal").fetchone()[0]
    out["intents"] = c.execute("SELECT COUNT(*) FROM intents").fetchone()[0]
print(json.dumps(out))
"""


def trial(n: int, args, port: int, ledger: str, rng: random.Random, horizon: float) -> dict:
    run = f"h{n}-{uuid.uuid4().hex[:8]}"
    ip = host_ip()
    p = subprocess.Popen(["wsl", "-d", DISTRO, "--exec", "bash", "-lc",
                          command("start", run, port, args, ip)],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline, t0 = time.time() + 60, None
    while time.time() < deadline and p.poll() is None and t0 is None:
        times = charge_times(ledger, run)
        t0 = times[0] if times else None
        time.sleep(0.005)
    rec = {"trial": n, "run": run, "courier": args.courier, "durable": args.durable,
           "calls": args.calls, "latency": args.latency, "think": args.think,
           "horizon": horizon}
    if t0 is None:
        p.kill()
        rec["status"] = "never-started"
        return rec
    offset = rng.uniform(0, horizon)
    rec["offset"] = offset
    while time.time() < t0 + offset and p.poll() is None:
        time.sleep(0.002)
    if p.poll() is not None:
        rec["status"] = "finished-before-kill"
        return rec
    killed = time.time()
    subprocess.run(["wsl", "--shutdown"], timeout=120, capture_output=True)
    rec["shutdown_s"] = time.time() - killed
    try:
        p.wait(timeout=30)
    except subprocess.TimeoutExpired:
        p.kill()
    # A request the dying VM had already sent still lands in the next moments.
    time.sleep(args.latency + 0.5)
    before = paidapi.charges(ledger, run)
    r0 = time.time()
    ip = host_ip()
    res = wsl(command("resume", run, port, args, ip), timeout=180)
    rec["recovery_s"] = time.time() - r0
    idx = paidapi.charges(ledger, run)
    seen = wsl(f"python3 -c {shlex.quote(READ)} ~/hc/{run}", timeout=60).stdout.strip()
    state = json.loads(seen.splitlines()[-1]) if seen else {}
    rec.update(status="ok" if res.returncode == 0 else "failed", rc=res.returncode,
               err=res.stderr[-400:] if res.returncode else "",
               charged_before_kill=len(before), charged=len(idx),
               duplicates=len(idx) - len(set(idx)), missing=args.calls - len(set(idx)),
               state=state)
    wsl(f"rm -rf ~/hc/{run}", timeout=60)
    return rec


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--courier", choices=["on", "off"], default="on")
    ap.add_argument("--durable", action="store_true")
    ap.add_argument("--trials", type=int, default=20)
    ap.add_argument("--calls", type=int, default=6)
    ap.add_argument("--latency", type=float, default=0.3)
    ap.add_argument("--think", type=float, default=0.1)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    done = 0
    if Path(args.out).exists():
        done = sum(json.loads(l).get("status") in ("ok", "failed")
                   for l in open(args.out, encoding="utf-8"))
    rng = random.Random(args.seed + done)
    scratch = Path(tempfile.mkdtemp(prefix="crashstudy-host-"))
    ledger = str(scratch / "ledger.db")
    paidapi.init(ledger)
    api = subprocess.Popen(
        [sys.executable, "-m", "benchmarks.crashstudy.paidapi", "--ledger", ledger,
         "--latency", str(args.latency), "--ignore-keys", "--host", "0.0.0.0"],
        cwd=ROOT, stdout=subprocess.PIPE,
        env={**__import__("os").environ, "PYTHONPATH": str(ROOT)})
    port = int(api.stdout.readline())
    drain(api.stdout)
    horizon = args.latency + (args.calls - 1) * (args.think + args.latency + 0.03)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(args.out, "a", encoding="utf-8") as out:
            n, tries = done, 0
            while n < args.trials and tries < args.trials * 3:
                tries += 1
                rec = trial(n, args, port, ledger, rng, horizon)
                out.write(json.dumps(rec) + "\n")
                out.flush()
                print(f"[{n}] {rec['status']:20} dup={rec.get('duplicates', '-')} "
                      f"missing={rec.get('missing', '-')} state={rec.get('state')} "
                      f"shutdown={rec.get('shutdown_s', 0):.2f}s", flush=True)
                n += rec["status"] in ("ok", "failed")
    finally:
        api.kill()
    return 0


if __name__ == "__main__":
    sys.exit(main())
