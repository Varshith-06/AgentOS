"""The same questions, asked of a real inference provider (Groq).

    GROQ_API_KEY=... python -m benchmarks.crashstudy.realprovider inflight --trials 40
    GROQ_API_KEY=... python -m benchmarks.crashstudy.realprovider idem --trials 10

Groq reports, on every response, how long until its per-model daily request
quota is full again (`x-ratelimit-reset-requests`). That duration is exactly
day/limit seconds per request admitted and does not tick down between requests,
so it is a running count of what the provider admitted: the difference between
two probes, minus the second probe itself, is what was admitted in between. No
guessing from token counts, which Groq reserves up front at max_tokens anyway.

`inflight`: a separate process opens a streaming completion, and is killed with
its whole tree at a uniformly random moment from just before the request is
written until well after the first token. Did the provider admit the request
the caller never saw finish?

`idem`: the same request twice with the same `Idempotency-Key`. Was the second
one admitted, and was it served as a new completion?
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx

API = "https://api.groq.com/openai/v1/chat/completions"
HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
LONG = ("Write the numbers from 1 to 400 in English words, one per line, "
        "with no other text.")


def _headers() -> dict:
    return {"Authorization": f"Bearer {os.environ['GROQ_API_KEY']}"}


def _duration(s: str) -> float:
    """'1m26.4s' / '592ms' / '2h3m4s' -> seconds."""
    total = 0.0
    for num, unit in re.findall(r"([\d.]+)(ms|h|m|s)", s):
        total += float(num) * {"ms": 1e-3, "s": 1, "m": 60, "h": 3600}[unit]
    return total


class Quota:
    """Requests admitted, read from the request-quota headers of a tiny probe."""

    def __init__(self, client: httpx.Client, model: str):
        self.client, self.model = client, model
        self.retried = 0  # probes that met a 5xx

    def probe(self) -> tuple[float, float, float]:
        """(when, deficit in requests, seconds per refilled request)."""
        for attempt in range(6):
            r = self.client.post(API, headers=_headers(), timeout=60, json={
                "model": self.model, "max_tokens": 1,
                "messages": [{"role": "user", "content": "1"}]})
            if r.status_code == 429 or r.status_code >= 500:
                # A 429 is refused. A 5xx may or may not have been admitted, so
                # any trial bracketed by one is reported (Quota.retried).
                self.retried += r.status_code >= 500
                time.sleep(float(r.headers.get("retry-after", 5)) + 1)
                continue
            r.raise_for_status()
            when = time.time()
            limit = int(r.headers["x-ratelimit-limit-requests"])
            per = 86400 / limit
            deficit = _duration(r.headers["x-ratelimit-reset-requests"]) / per
            return when, deficit, per
        raise RuntimeError("rate-limited on every probe attempt")

    @staticmethod
    def admitted(before: tuple, after: tuple) -> float:
        """Requests admitted strictly between two probes (the second excluded).

        The reset duration grows by exactly day/limit per admitted request. It
        gives one request back only after an idle gap longer than day/limit
        (86.4 s at 1000 a day, 12.3 s at 7000), so the count is exact while no
        two consecutive requests are further apart than that (`max_gap`).
        """
        (_, d0, _), (_, d1, _) = before, after
        return d1 - d0 - 1


def max_gap(before: tuple, after: tuple, log: list[dict]) -> float:
    """The longest stretch between consecutive requests from probe to probe."""
    t = sorted([before[0], after[0]] + [m["t"] for m in log if m["what"] == "send"])
    return max(b - a for a, b in zip(t, t[1:]))


# --- inflight ---------------------------------------------------------------

def _caller(model: str, max_tokens: int, out: str) -> None:
    """Child: connect, say READY, stream one completion, log what arrived."""
    log = open(out, "a", buffering=1, encoding="utf-8")

    def mark(what: str) -> None:
        log.write(json.dumps({"t": time.time(), "what": what}) + "\n")

    with httpx.Client(http2=False) as client:
        # Connection and TLS first, so the kill window starts at the request.
        client.get("https://api.groq.com/openai/v1/models", headers=_headers(), timeout=30)
        mark("ready")
        print("READY", flush=True)
        body = {"model": model, "max_tokens": max_tokens, "stream": True,
                "messages": [{"role": "user", "content": LONG}]}
        with client.stream("POST", API, headers=_headers(), json=body, timeout=120) as r:
            mark(f"status {r.status_code}")
            first = True
            for line in r.iter_lines():
                if line.startswith("data: ") and line != "data: [DONE]":
                    if first:
                        mark("first-token")
                        first = False
            mark("done")


def _caller_plain(model: str, out: str) -> None:
    """Child: the end-to-end client's own call (urllib, a fresh connection,
    not streamed), so the kill window includes connecting and queueing."""
    import urllib.request
    log = open(out, "a", buffering=1, encoding="utf-8")

    def mark(what: str) -> None:
        log.write(json.dumps({"t": time.time(), "what": what}) + "\n")

    body = json.dumps({"model": model, "max_tokens": 64,
                       "messages": [{"role": "user", "content": "Reply with the number 3 and nothing else."}]}).encode()
    req = urllib.request.Request(API, data=body, method="POST", headers={
        **_headers(), "Content-Type": "application/json", "User-Agent": "crashstudy/1.0"})
    mark("ready")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            resp.read()
            mark(f"status {resp.status}")
    except urllib.error.HTTPError as e:
        mark(f"status {e.code}")
    mark("done")


def inflight(args) -> None:
    import ctypes  # noqa: F401  (Windows job objects live in run.py)
    from benchmarks.crashstudy.run import kill_tree, popen
    delays = [float(x) for x in args.delays.split(",")] if args.delays else None

    rng = random.Random(args.seed)
    scratch = Path(args.out).with_suffix("")
    scratch.mkdir(parents=True, exist_ok=True)
    with httpx.Client() as client, open(args.out, "a", encoding="utf-8") as out:
        quota = Quota(client, args.model)
        for n in range(args.trials):
            log = scratch / f"caller-{n}.jsonl"
            log.unlink(missing_ok=True)
            before = quota.probe()
            env = dict(os.environ, PYTHONPATH=str(ROOT), PYTHONUTF8="1")
            child = (["_caller", args.model, str(args.max_tokens), str(log)] if args.mode == "stream"
                     else ["_caller_plain", args.model, str(log)])
            p = popen([args.python, "-m", "benchmarks.crashstudy.realprovider", *child], env,
                      scratch / f"caller-{n}.err")
            # Wait for READY in the child's log rather than its stdout.
            deadline = time.time() + 60
            while time.time() < deadline and not (log.exists() and "ready" in log.read_text()):
                time.sleep(0.005)
            ready = time.time()
            delay = delays[n % len(delays)] if delays else rng.uniform(0, args.window)
            while time.time() < ready + delay and p.poll() is None:
                time.sleep(0.002)
            finished = p.poll() is not None
            killed = kill_tree(p) if not finished else None
            time.sleep(args.settle)
            after = quota.probe()
            marks = [json.loads(l) for l in log.read_text().splitlines()] if log.exists() else []
            seen = {m["what"].split()[0]: m["t"] for m in marks}
            code = next((int(m["what"].split()[1]) for m in marks
                         if m["what"].startswith("status")), None)
            rec = {
                "trial": n, "model": args.model, "mode": args.mode, "delay": delay,
                "http_status": code,
                # A 429 is refused, not in flight: it says nothing about a lost reply.
                "throttled": code == 429,
                "killed": killed is not None,
                "kill_after_ready": (killed - seen["ready"]) if killed and "ready" in seen else None,
                "saw_status": "status" in seen, "saw_first_token": "first-token" in seen,
                "saw_done": "done" in seen,
                "first_token_after_ready": (seen["first-token"] - seen["ready"])
                if "first-token" in seen and "ready" in seen else None,
                "admitted": Quota.admitted(before, after), "probe_5xx_so_far": quota.retried,
            }
            out.write(json.dumps(rec) + "\n")
            out.flush()
            print(f"[{n + 1}/{args.trials}] delay={delay:.2f} http={code} "
                  f"first_token={rec['saw_first_token']!s:5} "
                  f"done={rec['saw_done']!s:5} admitted={rec['admitted']:+.3f}", flush=True)
            time.sleep(args.gap)


# --- e2e --------------------------------------------------------------------

def _calls_log(d: Path) -> list[dict]:
    f = d / "calls.jsonl"
    return [json.loads(l) for l in f.read_text(encoding="utf-8").splitlines()] if f.exists() else []


def _outstanding(log: list[dict], kill: float) -> list[list]:
    """Calls sent and not answered at the kill, as [i, seconds since send].

    A send answered with a 429 is not outstanding: the provider refused it and
    the client was waiting to send again.
    """
    last: dict[int, dict] = {}
    for m in log:
        if m["t"] <= kill:
            last[m["i"]] = m
    return sorted([i, kill - m["t"]] for i, m in last.items() if m["what"] == "send")


def e2e(args) -> None:
    """A framework's own workload and resume path, with every call a real completion.

    Duplicates are what the provider admitted beyond the workload's N calls.
    The kill is uniform over the work after the first call is sent, with the
    horizon measured from uncrashed calibration runs.
    """
    from benchmarks.crashstudy.run import NEEDS_TEMPORAL, kill_tree, start_temporal

    rng = random.Random(args.seed)
    scratch = Path(args.out).with_suffix("") / args.framework
    scratch.mkdir(parents=True, exist_ok=True)
    adapter = [args.python, "-m", "benchmarks.crashstudy.adapters", args.framework]
    temporal_proc, temporal = None, ""
    if args.framework in NEEDS_TEMPORAL:
        temporal_proc, temporal = start_temporal(args.python)
    gateway_proc, base = None, ""
    if args.gateway:
        from benchmarks.crashstudy.run import start_gateway
        gateway_proc, base = start_gateway(
            "https://api.groq.com/openai/v1",
            scratch / f"gateway-{uuid.uuid4().hex[:6]}.db")

    def env_for(run: str, d: Path, keyed: bool) -> dict:
        return dict(os.environ, PYTHONPATH=str(ROOT), PYTHONUTF8="1", CS_PROVIDER="groq",
                    CS_MODEL=args.model, CS_RUN=run, CS_DIR=str(d), CS_CALLS=str(args.calls),
                    CS_THINK=str(args.think), CS_KEYED="1" if keyed else "0", CS_PORT="0",
                    CS_TEMPORAL=temporal, CS_API_BASE=base,
                    # AgentOS only (see run.py): adoption, couriers, agent restarts.
                    CS_ADOPT="1" if args.adopt == "yes" else "0",
                    CS_COURIER="0" if args.courier == "off" else "1",
                    CS_RETRIES="3" if args.crash == "agent" else "0")

    try:
        _e2e(args, rng, scratch, adapter, env_for)
    finally:
        if temporal_proc is not None:
            kill_tree(temporal_proc)
        if gateway_proc is not None:
            gateway_proc.kill()


def _e2e(args, rng, scratch: Path, adapter: list, env_for) -> None:
    from benchmarks.crashstudy.run import LATE, agent_kill, kill_tree, popen, signals

    with httpx.Client() as client, open(args.out, "a", encoding="utf-8") as out:
        quota = Quota(client, args.model)
        # Calibration: run uncrashed, measure the call and the cycle.
        lat, cyc = [], []
        for c in range(args.calibrate):
            # A fresh directory: a reused one keeps an earlier invocation's calls.
            d = scratch / f"calib-{c}-{uuid.uuid4().hex[:6]}"
            d.mkdir(parents=True)
            before = quota.probe()
            res = subprocess.run(adapter + ["start"], timeout=300,
                                 env=env_for(f"calib-{c}-{uuid.uuid4().hex[:6]}", d, False))
            after = quota.probe()
            log = _calls_log(d)
            if res.returncode:
                print(f"calibration {c} failed (rc {res.returncode}); timing kept, count not "
                      f"checked", flush=True)
            # A throttled call is sent again; time it from the send that was answered.
            last, firsts = {}, {}
            for m in log:
                if m["what"] == "send":
                    last[m["i"]] = m["t"]
                    firsts.setdefault(m["i"], m["t"])
                elif m["what"] == "reply" and m["i"] in last:
                    lat.append(m["t"] - last[m["i"]])
            starts = sorted(firsts.values())
            cyc += [b - a for a, b in zip(starts, starts[1:])]
            print(f"calibration {c}: admitted={Quota.admitted(before, after):+.3f} "
                  f"(expect {args.calls})", flush=True)
            time.sleep(args.gap)
        latency, cycle = sorted(lat)[len(lat) // 2], sorted(cyc)[len(cyc) // 2]
        horizon = latency + (args.calls - 1) * cycle
        print(f"median call {latency:.3f}s, cycle {cycle:.3f}s, horizon {horizon:.2f}s", flush=True)

        plan = [(n, k) for n in range(args.trials) for k in args.keyed]
        for n, keyed in plan:
            run = f"{args.framework}-{'k' if keyed else 'u'}-{n}-{uuid.uuid4().hex[:6]}"
            d = scratch / run
            d.mkdir(parents=True)
            env = env_for(run, d, keyed)
            before = quota.probe()
            errors_before = quota.retried
            p = popen(adapter + ["start"], env, d / "start.err",
                      breakaway=args.courier == "on")
            deadline = time.time() + 120
            t0 = None
            while time.time() < deadline and p.poll() is None and t0 is None:
                sends = [m for m in _calls_log(d) if m["what"] == "send"]
                t0 = sends[0]["t"] if sends else None
                time.sleep(0.003)
            offset = rng.uniform(0, horizon)
            while t0 is not None and time.time() < t0 + offset and p.poll() is None:
                time.sleep(0.002)
            finished = p.poll() is not None
            if args.crash == "agent":
                # Only the agent's process dies; the runtime restarts it and
                # runs on, so the run that was started is the one that ends.
                killed = None if finished else agent_kill(d)
                res = p
                try:
                    res.wait(timeout=300)
                    hung = False
                except subprocess.TimeoutExpired:
                    hung = True
                kill_tree(res)
                res.stderr = (d / "start.err").read_bytes()
            else:
                killed = None if finished else kill_tree(p)
                time.sleep(0.2)
                # A resume that hangs is a trial too: record it, kill its whole
                # tree so nothing it started keeps spending quota, and go on.
                res = popen(adapter + ["resume"], env, d / "resume.err")
                try:
                    res.wait(timeout=300)
                    hung = False
                except subprocess.TimeoutExpired:
                    kill_tree(res)
                    hung = True
                res.stderr = (d / "resume.err").read_bytes()
            time.sleep(args.settle)
            after = quota.probe()
            log = _calls_log(d)
            admitted = Quota.admitted(before, after)
            outstanding = _outstanding(log, killed) if killed else None
            status = "ok" if res.returncode == 0 else "failed"
            if finished:
                status = "finished-before-kill"
            elif killed is None:
                status = "no-agent"  # an agent crash found no agent to kill
            elif t0 is not None and killed - (t0 + offset) > LATE:
                status = "late"
            if any(m["what"] == "server-error" for m in log) or quota.retried > errors_before:
                # A 5xx may have been admitted: the count can't separate it from a duplicate.
                status = "provider-error"
            if hung:
                status = "resume-timeout"
            rec = {"framework": args.framework, "keyed": keyed, "trial": n, "run": run,
                   "gateway": args.gateway, "crash": args.crash,
                   "adopt": args.adopt == "yes", "courier": args.courier,
                   "model": args.model, "calls": args.calls, "think": args.think,
                   "latency": latency, "cycle": cycle, "horizon": horizon, "offset": offset,
                   "status": status, "rc": res.returncode,
                   "err": res.stderr.decode("utf-8", "replace")[-400:] if res.returncode else "",
                   "probe_5xx_so_far": quota.retried, "admitted": admitted, "duplicates": round(admitted) - args.calls,
                   # The count is exact only if no gap between requests outlasted
                   # one refill interval (see Quota.admitted).
                   "max_gap": max_gap(before, after, log), "refill": before[2],
                   "sends_logged": sum(m["what"] == "send" for m in log),
                   "throttled": sum(m["what"] == "throttled" for m in log),
                   "server_errors": sum(m["what"] == "server-error" for m in log),
                   "killed_at": killed, "outstanding_at_kill": outstanding,
                   "signals": signals(d)}
            out.write(json.dumps(rec) + "\n")
            out.flush()
            print(f"[{plan.index((n, keyed)) + 1}/{len(plan)}] key={'y' if keyed else 'n'} "
                  f"{status:8} admitted={admitted:+.3f} dup={rec['duplicates']} "
                  f"outstanding={outstanding}", flush=True)
            time.sleep(args.gap)


# --- idem -------------------------------------------------------------------

def idem(args) -> None:
    with httpx.Client() as client, open(args.out, "a", encoding="utf-8") as out:
        quota = Quota(client, args.model)
        for n in range(args.trials):
            key = str(uuid.uuid4())
            body = {"model": args.model, "max_tokens": 300, "temperature": 1.0,
                    "messages": [{"role": "user", "content": "Name a random animal."}]}
            before = quota.probe()
            replies = []
            for _ in range(2):
                r = client.post(API, headers={**_headers(), "Idempotency-Key": key},
                                json=body, timeout=60)
                j = r.json()
                replies.append({"status": r.status_code, "id": j.get("id"),
                                "text": ((j.get("choices") or [{}])[0].get("message") or {}).get("content"),
                                "idem_headers": {k: v for k, v in r.headers.items()
                                                 if "idempot" in k.lower()}})
            after = quota.probe()
            rec = {"trial": n, "model": args.model, "key": key, "replies": replies,
                   "same_id": replies[0]["id"] == replies[1]["id"], "probe_5xx_so_far": quota.retried,
                   "admitted": Quota.admitted(before, after)}
            out.write(json.dumps(rec) + "\n")
            out.flush()
            print(f"[{n + 1}/{args.trials}] same_id={rec['same_id']} admitted={rec['admitted']:+.3f} "
                  f"texts={[x['text'] for x in replies]}", flush=True)
            time.sleep(args.gap)


def summary(args) -> None:
    """The numbers the paper quotes, from whichever result files exist."""
    from benchmarks.crashstudy.analyze import wilson
    res = Path(args.out).parent

    def rows(name: str) -> list[dict]:
        f = res / name
        return [json.loads(l) for l in f.read_text(encoding="utf-8").splitlines()] if f.exists() else []

    for model in sorted({r["model"] for r in rows("real-inflight.jsonl")}):
        rs = [r for r in rows("real-inflight.jsonl") if r["model"] == model and not r["throttled"]]
        cut = [r for r in rs if r["killed"] and not r["saw_done"]]
        whole = [r for r in rs if r["saw_done"]]
        n_adm = sum(round(r["admitted"]) == 1 for r in cut)
        pre_hdr = [r for r in cut if not r["saw_status"]]
        pre_tok = [r for r in cut if not r["saw_first_token"]]
        print(f"inflight {model}: killed mid-request {len(cut)}, admitted {n_adm} "
              f"(before headers: {sum(round(r['admitted']) == 1 for r in pre_hdr)}/{len(pre_hdr)}, "
              f"before first token: {sum(round(r['admitted']) == 1 for r in pre_tok)}/{len(pre_tok)}, "
              f"earliest kill {1000 * min((r['kill_after_ready'] for r in cut), default=0):.0f} ms); "
              f"completed {len(whole)}, admitted {sum(round(r['admitted']) == 1 for r in whole)}; "
              f"throttled excluded {sum(r['throttled'] for r in rows('real-inflight.jsonl') if r['model'] == model)}")
    adm = [r for r in rows("real-admission.jsonl") if not r["throttled"]]
    for delay in sorted({r["delay"] for r in adm}):
        rs = [r for r in adm if r["delay"] == delay]
        cut = [r for r in rs if r["killed"] and not r["saw_done"]]
        print(f"admission {rs[0]['model']} plain, killed {1000 * delay:.0f} ms after the request began: "
              f"admitted {sum(round(r['admitted']) == 1 for r in cut)}/{len(cut)} "
              f"(finished before the kill: {len(rs) - len(cut)})")
    for model in sorted({r["model"] for r in rows("real-idem.jsonl")}):
        rs = [r for r in rows("real-idem.jsonl") if r["model"] == model
              and all(x["status"] == 200 for x in r["replies"])]
        print(f"idem {model}: pairs {len(rs)}, both admitted {sum(round(r['admitted']) == 2 for r in rs)}, "
              f"same completion id {sum(r['same_id'] for r in rs)}, "
              f"identical text {sum(r['replies'][0]['text'] == r['replies'][1]['text'] for r in rs)}, "
              f"idempotency headers seen {sum(bool(x['idem_headers']) for r in rs for x in r['replies'])}")
    e = rows("real-e2e.jsonl")

    def outstanding(r: dict) -> list[list]:
        log = _calls_log(res / "real-e2e" / r["framework"] / r["run"])
        t0 = next(m["t"] for m in log if m["what"] == "send")
        return _outstanding(log, r.get("killed_at") or t0 + r["offset"])

    for fw in sorted({r["framework"] for r in e}):
        for keyed in (False, True):
            rs = [r for r in e if r["framework"] == fw and r["keyed"] == keyed]
            ok = [r for r in rs if r["status"] == "ok"]
            k = sum(r["duplicates"] > 0 for r in ok)
            lo, hi = wilson(k, len(ok))
            out = {r["run"]: outstanding(r) for r in ok}
            held = [r for r in ok if out[r["run"]]]
            missed = sorted(round(1000 * out[r["run"]][0][1]) for r in held if not r["duplicates"])
            print(f"e2e {fw} key={'y' if keyed else 'n'}: crashes {len(ok)}, P(dup) {k}/{len(ok)} = "
                  f"{k / max(1, len(ok)):.2f} [{lo:.2f}, {hi:.2f}], max dup "
                  f"{max((r['duplicates'] for r in ok), default=0)}, call outstanding at kill "
                  f"{len(held)} -> dup {sum(r['duplicates'] > 0 for r in held)} (no dup when killed "
                  f"{missed} ms after the send), none outstanding -> dup "
                  f"{sum(r['duplicates'] > 0 for r in ok if not out[r['run']])}; excluded "
                  f"{len(rs) - len(ok)} ({', '.join(sorted({r['status'] for r in rs if r['status'] != 'ok'}))})")


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "_caller":
        _caller(sys.argv[2], int(sys.argv[3]), sys.argv[4])
        return 0
    if len(sys.argv) > 1 and sys.argv[1] == "_caller_plain":
        _caller_plain(sys.argv[2], sys.argv[3])
        return 0
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("experiment", choices=["inflight", "idem", "e2e", "summary"])
    ap.add_argument("--framework", default="langgraph-node", help="e2e: which adapter")
    ap.add_argument("--calls", type=int, default=6, help="e2e: calls per run")
    ap.add_argument("--think", type=float, default=0.1, help="e2e: local work per call")
    ap.add_argument("--calibrate", type=int, default=2, help="e2e: uncrashed runs first")
    ap.add_argument("--keyed", default="no,yes",
                    help="e2e: key modes to interleave (no, yes, or both)")
    ap.add_argument("--model", default="openai/gpt-oss-20b")
    ap.add_argument("--trials", type=int, default=20)
    ap.add_argument("--max-tokens", type=int, default=800,
                    help="Groq reserves prompt + max_tokens against its per-minute quota")
    ap.add_argument("--mode", choices=["stream", "plain"], default="stream",
                    help="inflight: a streamed completion on an open connection, or the "
                    "e2e client's own call (urllib, fresh connection, not streamed)")
    ap.add_argument("--delays", help="inflight: fixed kill delays (s), cycled, instead of --window")
    ap.add_argument("--window", type=float, default=1.5,
                    help="kill uniformly within this many seconds of READY")
    ap.add_argument("--settle", type=float, default=1.0,
                    help="wait after the kill before probing, so the request lands")
    ap.add_argument("--gap", type=float, default=12.0,
                    help="pause between trials, to stay inside the per-minute quotas")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--gateway", action="store_true",
                    help="e2e: send every call through an AgentOS gateway")
    ap.add_argument("--crash", choices=["runtime", "agent"], default="runtime",
                    help="e2e: kill the whole tree, or (AgentOS) only the agent's process")
    ap.add_argument("--adopt", choices=["yes", "no"], default="yes",
                    help="e2e, AgentOS: give a restarted agent its call in flight")
    ap.add_argument("--courier", choices=["off", "on", "contained"], default="off",
                    help="e2e, AgentOS: carry calls by courier; contained ones die too")
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("--out", default=str(HERE / "results" / "real" / "real.jsonl"))
    args = ap.parse_args()
    args.keyed = [k == "yes" for k in args.keyed.split(",")]
    {"inflight": inflight, "idem": idem, "e2e": e2e, "summary": summary}[args.experiment](args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
