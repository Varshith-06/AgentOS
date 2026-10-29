"""The provider-side questions, asked of two more providers.

    OPENROUTER_API_KEY=... python -m benchmarks.crashstudy.otherproviders or-inflight --trials 20
    OPENROUTER_API_KEY=... python -m benchmarks.crashstudy.otherproviders or-idem --trials 10
    GOOGLE_API_KEY=...     python -m benchmarks.crashstudy.otherproviders gemini-idem --trials 10

OpenRouter counts the free-model requests a key has made today
(`free_model_daily_requests.used` on GET /api/v1/key). The count is exact but
lags by up to a minute, so each trial waits until it has settled. OpenRouter
also keeps a record of every generation, which says whether it was cancelled
and how many tokens were produced.

Google AI Studio exposes no request count, so for Gemini only the replies can
be compared: two completions with different response ids were each generated.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
import uuid
from pathlib import Path

import httpx

from benchmarks.crashstudy.realprovider import HERE, LONG, ROOT

OR = "https://openrouter.ai/api/v1"
GEMINI = "https://generativelanguage.googleapis.com/v1beta/models"


def _or_headers() -> dict:
    return {"Authorization": f"Bearer {os.environ['OPENROUTER_API_KEY']}"}


def or_used(client: httpx.Client) -> int:
    r = client.get(f"{OR}/key", headers=_or_headers(), timeout=30)
    return r.json()["data"]["free_model_daily_requests"]["used"]


def or_settled(client: httpx.Client, before: int, least: float = 150) -> int:
    """The request count once it has stopped moving: at least `least` seconds
    after the last request, and the same on three reads 30 s apart. The count
    has lagged by over 80 s, so a shorter wait credits a request to the next
    trial."""
    time.sleep(least)
    last, same = or_used(client), 0
    while same < 2:
        time.sleep(30)
        now = or_used(client)
        same = same + 1 if now == last else 0
        last = now
    return last - before


def or_generation(client: httpx.Client, gen: str | None) -> dict | None:
    if not gen:
        return None
    for _ in range(18):
        r = client.get(f"{OR}/generation", headers=_or_headers(), params={"id": gen}, timeout=30)
        if r.status_code == 200:
            d = r.json()["data"]
            return {k: d.get(k) for k in ("cancelled", "streamed", "tokens_completion",
                                          "native_tokens_completion", "generation_time",
                                          "finish_reason", "model")}
        time.sleep(10)
    return None


def _caller(model: str, out: str) -> None:
    """Child: connect, say READY, stream one completion, log what arrived."""
    log = open(out, "a", buffering=1, encoding="utf-8")

    def mark(what: str, **extra) -> None:
        log.write(json.dumps({"t": time.time(), "what": what, **extra}) + "\n")

    with httpx.Client(http2=False) as client:
        # Connection and TLS first, so the kill window starts at the request.
        client.get(f"{OR}/models", timeout=30)
        mark("ready")
        body = {"model": model, "max_tokens": 1500, "stream": True,
                "messages": [{"role": "user", "content": LONG}]}
        with client.stream("POST", f"{OR}/chat/completions", headers=_or_headers(),
                           json=body, timeout=180) as r:
            mark(f"status {r.status_code}", gen=r.headers.get("x-generation-id"))
            first = True
            for line in r.iter_lines():
                if line.startswith("data: ") and line != "data: [DONE]":
                    if first:
                        mark("first-token")
                        first = False
            mark("done")


def or_inflight(args) -> None:
    """Kill a client mid-request. Did OpenRouter still count the request, and
    did it cancel the generation upstream?"""
    from benchmarks.crashstudy.run import kill_tree, popen

    rng = random.Random(args.seed)
    scratch = Path(args.out).with_suffix("")
    scratch.mkdir(parents=True, exist_ok=True)
    with httpx.Client() as client, open(args.out, "a", encoding="utf-8") as out:
        for n in range(args.trials):
            log = scratch / f"caller-{uuid.uuid4().hex[:8]}.jsonl"
            before = or_used(client)
            env = dict(os.environ, PYTHONPATH=str(ROOT), PYTHONUTF8="1")
            p = popen([args.python, "-m", "benchmarks.crashstudy.otherproviders", "_caller",
                       args.model, str(log)], env, log.with_suffix(".err"))
            deadline = time.time() + 60
            while time.time() < deadline and not (log.exists() and "ready" in log.read_text()):
                time.sleep(0.005)
            ready = time.time()
            delay = rng.uniform(0, args.window)
            while time.time() < ready + delay and p.poll() is None:
                time.sleep(0.002)
            finished = p.poll() is not None
            killed = kill_tree(p) if not finished else None
            admitted = or_settled(client, before)
            marks = [json.loads(l) for l in log.read_text().splitlines()] if log.exists() else []
            seen = {m["what"].split()[0]: m for m in marks}
            status = seen.get("status")
            code = int(status["what"].split()[1]) if status else None
            gen = status.get("gen") if status else None
            rec = {"provider": "openrouter", "trial": n, "model": args.model, "delay": delay,
                   "http_status": code, "throttled": code == 429, "killed": killed is not None,
                   "saw_status": status is not None, "saw_first_token": "first-token" in seen,
                   "saw_done": "done" in seen, "admitted": admitted, "generation": gen,
                   "record": or_generation(client, gen)}
            out.write(json.dumps(rec) + "\n")
            out.flush()
            print(f"[{n + 1}/{args.trials}] delay={delay:.2f} http={code} "
                  f"first_token={rec['saw_first_token']!s:5} done={rec['saw_done']!s:5} "
                  f"admitted={admitted} record={rec['record']}", flush=True)


def or_idem(args) -> None:
    """The same request twice with one Idempotency-Key: counted once or twice?"""
    with httpx.Client() as client, open(args.out, "a", encoding="utf-8") as out:
        for n in range(args.trials):
            key = str(uuid.uuid4())
            before = or_used(client)
            replies = []
            for _ in range(2):
                # The model reasons first; 200 tokens often ended before any text.
                r = client.post(f"{OR}/chat/completions", timeout=120,
                                headers={**_or_headers(), "Idempotency-Key": key},
                                json={"model": args.model, "max_tokens": 800, "messages": [
                                    {"role": "user", "content": "Name one animal. One word."}]})
                body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
                replies.append({"status": r.status_code, "id": body.get("id"),
                                "text": ((body.get("choices") or [{}])[0].get("message") or {}).get("content"),
                                "idem_headers": {k: v for k, v in r.headers.items() if "idempot" in k.lower()}})
            admitted = or_settled(client, before)
            rec = {"provider": "openrouter", "trial": n, "model": args.model, "key": key,
                   "replies": replies, "same_id": replies[0]["id"] == replies[1]["id"],
                   "admitted": admitted}
            out.write(json.dumps(rec) + "\n")
            out.flush()
            print(f"[{n + 1}/{args.trials}] admitted={admitted} same_id={rec['same_id']} "
                  f"texts={[r['text'] for r in replies]}", flush=True)


def gemini_idem(args) -> None:
    """The same request twice with one Idempotency-Key: one generation or two?"""
    h = {"x-goog-api-key": os.environ["GOOGLE_API_KEY"]}
    with httpx.Client() as client, open(args.out, "a", encoding="utf-8") as out:
        for n in range(args.trials):
            key = str(uuid.uuid4())
            replies = []
            for _ in range(2):
                r = client.post(f"{GEMINI}/{args.model}:generateContent", timeout=120,
                                headers={**h, "Idempotency-Key": key},
                                json={"contents": [{"parts": [{"text": "Name one animal. One word."}]}]})
                body = r.json() if r.status_code == 200 else {}
                parts = ((body.get("candidates") or [{}])[0].get("content") or {}).get("parts") or []
                replies.append({"status": r.status_code, "id": body.get("responseId"),
                                "text": "".join(p.get("text", "") for p in parts if not p.get("thought")),
                                "idem_headers": {k: v for k, v in r.headers.items() if "idempot" in k.lower()}})
            rec = {"provider": "google", "trial": n, "model": args.model, "key": key,
                   "replies": replies, "same_id": replies[0]["id"] == replies[1]["id"]}
            out.write(json.dumps(rec) + "\n")
            out.flush()
            print(f"[{n + 1}/{args.trials}] same_id={rec['same_id']} "
                  f"texts={[r['text'] for r in replies]}", flush=True)
            time.sleep(args.gap)


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "_caller":
        _caller(sys.argv[2], sys.argv[3])
        return 0
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("experiment", choices=["or-inflight", "or-idem", "gemini-idem"])
    ap.add_argument("--model")
    ap.add_argument("--trials", type=int, default=10)
    ap.add_argument("--window", type=float, default=3.0,
                    help="or-inflight: kill uniformly within this many seconds of READY")
    ap.add_argument("--gap", type=float, default=5.0)
    ap.add_argument("--seed", type=int, default=11)
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("--out")
    args = ap.parse_args()
    defaults = {"or-inflight": ("nvidia/nemotron-3.5-lightning:free", "real-or-inflight"),
                "or-idem": ("nvidia/nemotron-3.5-lightning:free", "real-or-idem"),
                "gemini-idem": ("gemini-3.5-flash-lite", "real-gemini-idem")}
    model, name = defaults[args.experiment]
    args.model = args.model or model
    args.out = args.out or str(HERE / "results" / "real" / f"{name}.jsonl")
    {"or-inflight": or_inflight, "or-idem": or_idem,
     "gemini-idem": gemini_idem}[args.experiment](args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
