"""The crash study on models from other providers: slower, paid, and not on Groq.

    REQUESTY_API_KEY=... python -m benchmarks.crashstudy.multi --via requesty \
        --model anthropic/claude-haiku-4-5 --framework agentos --courier on --trials 10 --out F

Groq's quota lets `realprovider` count what the provider admitted. Nothing here
offers that, so this counts what the provider was sent, from the callers' own
log, flushed before each send. Sends bound the bill from above: a run that sent
each call once was billed once wherever it ran, whatever the provider does. A
run that sent one twice paid for it twice if the provider admits a request whose
caller has died, which the provider probes test separately.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import subprocess
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from benchmarks.crashstudy.realprovider import _calls_log, _outstanding  # noqa: E402
from benchmarks.crashstudy.run import (LATE, NEEDS_TEMPORAL, agent_kill, kill_tree,  # noqa: E402
                                       popen, replies, signals, start_temporal)

VIA = {  # name: (OpenAI-compatible base URL, key variable)
    "groq": ("https://api.groq.com/openai/v1", "GROQ_API_KEY"),
    "cohere": ("https://api.cohere.com/compatibility/v1", "COHERE_API_KEY"),
    "google": ("https://generativelanguage.googleapis.com/v1beta/openai", "GEMINI_API_KEY"),
    "requesty": ("https://router.requesty.ai/v1", "REQUESTY_API_KEY"),
    "bazaarlink": ("https://bazaarlink.ai/v1", "BAZAARLINK_API_KEY"),
    "openrouter": ("https://openrouter.ai/api/v1", "OPENROUTER_API_KEY"),
}


def tally(log: list[dict], calls: int) -> dict:
    """Sends a provider may have billed: a refusal (429) was not."""
    sends = sum(m["what"] == "send" for m in log)
    refused = sum(m["what"] == "throttled" for m in log)
    errors = sum(m["what"] == "server-error" for m in log)
    billable = sends - refused
    usage = [m["usage"] for m in log if m["what"] == "reply" and m.get("usage")]
    return {"sends": sends, "refused": refused, "server_errors": errors,
            "billable_sends": billable, "resent": billable - calls,
            "called": len({m["i"] for m in log if m["what"] == "send"}),
            "prompt_tokens": sum(u.get("prompt_tokens") or 0 for u in usage),
            "completion_tokens": sum(u.get("completion_tokens") or 0 for u in usage)}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--via", required=True, choices=sorted(VIA))
    ap.add_argument("--model", required=True)
    ap.add_argument("--framework", default="agentos")
    ap.add_argument("--crash", choices=["runtime", "agent"], default="runtime")
    ap.add_argument("--adopt", choices=["yes", "no"], default="yes")
    ap.add_argument("--courier", choices=["off", "on", "contained"], default="off")
    ap.add_argument("--drift", choices=["tail", "all"])
    ap.add_argument("--no-fingerprint", action="store_true")
    ap.add_argument("--task", action="store_true",
                    help="a real task: the agent builds each prompt from its own notes")
    ap.add_argument("--trials", type=int, default=10)
    ap.add_argument("--calls", type=int, default=6)
    ap.add_argument("--think", type=float, default=0.1)
    ap.add_argument("--words", type=int, default=150, help="how long a reply to ask for")
    ap.add_argument("--max-tokens", type=int, default=400)
    ap.add_argument("--reasoning", help="reasoning_effort, for models that take it")
    ap.add_argument("--calibrate", type=int, default=2)
    ap.add_argument("--gap", type=float, default=1.0, help="seconds between trials")
    ap.add_argument("--settle", type=float, default=3.0)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--timeout", type=float, default=400)
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    base, var = VIA[args.via]
    rng = random.Random(args.seed)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    scratch = out.with_suffix("") / args.framework
    scratch.mkdir(parents=True, exist_ok=True)
    adapter = [args.python, "-m", "benchmarks.crashstudy.adapters", args.framework]
    temporal_proc, temporal = None, ""
    if args.framework in NEEDS_TEMPORAL:
        temporal_proc, temporal = start_temporal(args.python)

    def env_for(run: str, d: Path) -> dict:
        env = dict(os.environ, PYTHONPATH=str(ROOT), PYTHONUTF8="1", CS_PROVIDER="groq",
                   CS_API_BASE=base, CS_KEY_VAR=var, CS_MODEL=args.model, CS_RUN=run,
                   CS_DIR=str(d), CS_CALLS=str(args.calls), CS_THINK=str(args.think),
                   CS_KEYED="0", CS_PORT="0", CS_TEMPORAL=temporal,
                   CS_WORDS=str(args.words), CS_MAX_TOKENS=str(args.max_tokens),
                   CS_ACTIVITY_TIMEOUT="120", CS_TOOL_TIMEOUT="300",
                   CS_ADOPT="1" if args.adopt == "yes" else "0",
                   CS_COURIER="0" if args.courier == "off" else "1",
                   AGENTOS_COURIER_DETACH="0" if args.courier == "contained" else "1",
                   CS_RETRIES="3" if args.crash == "agent" else "0")
        if args.reasoning:
            env["CS_REASONING"] = args.reasoning
        if args.drift:
            env["CS_DRIFT"] = args.drift
        if args.task:
            env["CS_TASK"] = "1"
        if args.no_fingerprint:
            env["CS_NOFP"] = "1"
        return env

    done = 0
    if out.exists():
        done = sum(json.loads(l)["status"] in ("ok", "failed") for l in open(out, encoding="utf-8"))

    try:
        # Calibration: the call and the cycle, uncrashed.
        lat, cyc = [], []
        for c in range(args.calibrate):
            d = scratch / f"calib-{c}-{uuid.uuid4().hex[:6]}"
            d.mkdir(parents=True)
            subprocess.run(adapter + ["start"], env=env_for(d.name, d), timeout=args.timeout,
                           cwd=ROOT, capture_output=True)
            last, firsts = {}, {}
            for m in _calls_log(d):
                if m["what"] == "send":
                    last[m["i"]] = m["t"]
                    firsts.setdefault(m["i"], m["t"])
                elif m["what"] == "reply" and m["i"] in last:
                    lat.append(m["t"] - last[m["i"]])
            starts = sorted(firsts.values())
            cyc += [b - a for a, b in zip(starts, starts[1:])]
        if not lat or not cyc:
            print(f"{args.via} {args.model} {args.framework}: calibration made no calls", flush=True)
            return 1
        latency, cycle = sorted(lat)[len(lat) // 2], sorted(cyc)[len(cyc) // 2]
        horizon = latency + (args.calls - 1) * cycle
        print(f"{args.via} {args.model} {args.framework}: call {latency:.2f}s cycle {cycle:.2f}s "
              f"horizon {horizon:.1f}s", flush=True)

        n, tries = done, 0
        with open(out, "a", encoding="utf-8") as sink:
            while n < args.trials and tries < 3 * args.trials:
                tries += 1
                run = f"{args.framework}-{n}-{uuid.uuid4().hex[:6]}"
                d = scratch / run
                d.mkdir(parents=True)
                env = env_for(run, d)
                p = popen(adapter + ["start"], env, d / "start.err",
                          breakaway=args.courier == "on")
                deadline, t0 = time.time() + 120, None
                while time.time() < deadline and p.poll() is None and t0 is None:
                    sends = [m for m in _calls_log(d) if m["what"] == "send"]
                    t0 = sends[0]["t"] if sends else None
                    time.sleep(0.003)
                offset = rng.uniform(0, horizon)
                while t0 is not None and time.time() < t0 + offset and p.poll() is None:
                    time.sleep(0.002)
                finished = p.poll() is not None
                hung = False
                r0 = time.perf_counter()
                if args.crash == "agent":
                    killed = None if finished else agent_kill(d)
                    try:
                        p.wait(timeout=args.timeout)
                    except subprocess.TimeoutExpired:
                        hung = True
                    kill_tree(p)
                    res = p
                    errs = d / "start.err"
                else:
                    killed = None if finished else kill_tree(p)
                    time.sleep(0.2)
                    r0 = time.perf_counter()
                    res = popen(adapter + ["resume"], env, d / "resume.err")
                    try:
                        res.wait(timeout=args.timeout)
                    except subprocess.TimeoutExpired:
                        kill_tree(res)
                        hung = True
                    errs = d / "resume.err"
                recovery = time.perf_counter() - r0
                time.sleep(args.settle if args.courier != "off" else 0.5)
                log = _calls_log(d)
                t = tally(log, args.calls)
                # A kernel exits 0 even if its agent failed: count the answers.
                answered = replies(d)["answered"]
                status = "ok" if res.returncode == 0 and answered == args.calls else "failed"
                if status == "failed" and t["refused"]:
                    status = "provider-error"  # throttled until it gave up, not the crash's doing
                if t0 is None:
                    status = "never-started"
                elif finished:
                    status = "finished-before-kill"
                elif killed is None:
                    status = "no-agent"
                elif killed - (t0 + offset) > LATE:
                    status = "late"
                if t["server_errors"]:
                    status = "provider-error"
                if hung:
                    status = "resume-timeout"
                rec = {"framework": args.framework, "via": args.via, "model": args.model,
                       "trial": n, "run": run, "crash": args.crash, "adopt": args.adopt == "yes",
                       "courier": args.courier, "drift": args.drift,
                       "fingerprint": not args.no_fingerprint, "calls": args.calls,
                       "think": args.think, "words": args.words, "latency": latency,
                       "cycle": cycle, "horizon": horizon, "offset": offset, "status": status,
                       "rc": res.returncode, "recovery_s": recovery,
                       "err": errs.read_bytes()[-300:].decode("utf-8", "replace") if res.returncode else "",
                       "outstanding_at_kill": _outstanding(log, killed) if killed else None,
                       "signals": signals(d), **t, **replies(d)}
                sink.write(json.dumps(rec) + "\n")
                sink.flush()
                n += status in ("ok", "failed")
                print(f"[{n}/{args.trials}] {args.via} {args.framework} {status:20} "
                      f"sends={t['billable_sends']} resent={t['resent']} wrong={rec['wrong_replies']} "
                      f"inflight={bool(rec['outstanding_at_kill'])}", flush=True)
                time.sleep(args.gap)
    finally:
        if temporal_proc is not None:
            kill_tree(temporal_proc)
    return 0


if __name__ == "__main__":
    sys.exit(main())
