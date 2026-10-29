"""The one call every framework makes: blocking, stdlib, optionally keyed."""

from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.request

# Temporal's start-to-close timeout (s): sized to the stand-in provider's call,
# raised for real completions, whose latency has a long tail.
ACTIVITY_TIMEOUT = float(os.environ.get("CS_ACTIVITY_TIMEOUT", "3"))


def charge(i: int, key: str | None = None) -> dict:
    """One billable call. Think time (local work after the reply) is separate."""
    if os.environ.get("CS_PROVIDER") == "groq":
        return _groq(i, key)
    body = json.dumps({"run": os.environ["CS_RUN"], "i": i}).encode()
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Idempotency-Key"] = key
    req = urllib.request.Request(
        f"http://127.0.0.1:{os.environ['CS_PORT']}/charge", data=body,
        headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read())


def _mark(what: str, i: int, **extra) -> None:
    # Appended and flushed before and after the call, so a kill leaves a record
    # of which call was outstanding.
    with open(os.path.join(os.environ["CS_DIR"], "calls.jsonl"), "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"t": time.time(), "what": what, "i": i, **extra}) + "\n")


# With CS_CHAIN=1 each prompt carries the previous reply, as an agent's next
# step depends on its last one. Held in the process, like a loop's local
# variable: a unit that re-runs the loop regenerates it.
_last: str | None = None


def _prompt(i: int) -> str:
    if os.environ.get("CS_CHAIN") == "1":
        return (f"Step {i} of a task. The previous step said: {_last or '(nothing yet)'} "
                "In one short sentence, say what to do next.")
    return f"Reply with the number {i} and nothing else."


def _groq(i: int, key: str | None) -> dict:
    """The same call against a real model. Groq accepts the header and ignores it."""
    global _last
    model = os.environ.get("CS_MODEL", "openai/gpt-oss-120b")
    prompt = _prompt(i)
    request = {"model": model, "max_tokens": 64,
               "messages": [{"role": "user", "content": prompt}]}
    if model.startswith(("openai/gpt-oss", "qwen/")):  # reasoning models; others reject it
        request["reasoning_effort"] = "low"
    body = json.dumps(request).encode()
    # Groq's edge refuses urllib's default User-Agent with a 403.
    headers = {"Content-Type": "application/json", "User-Agent": "crashstudy/1.0",
               "Authorization": f"Bearer {os.environ['GROQ_API_KEY']}"}
    if key:
        headers["Idempotency-Key"] = key
    for _ in range(5):
        # CS_API_BASE puts a gateway (agent gateway) between the run and Groq.
        base = os.environ.get("CS_API_BASE") or "https://api.groq.com/openai/v1"
        req = urllib.request.Request(f"{base}/chat/completions",
                                     data=body, headers=headers, method="POST")
        # A fingerprint of the request, to tell a repeat from a different call.
        _mark("send", i, h=hashlib.sha1(prompt.encode()).hexdigest()[:12])
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                reply = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if e.code == 429:  # refused, not admitted: wait and resend
                _mark("throttled", i)
            elif e.code >= 500:  # may or may not have been admitted; the run is flagged
                _mark("server-error", i)
            else:
                raise
            time.sleep(float(e.headers.get("retry-after") or 5) + 1)
            continue
        _mark("reply", i)
        _last = (reply["choices"][0]["message"].get("content") or "").strip()
        return {"i": i, "id": reply.get("id")}
    raise RuntimeError("rate-limited five times running")


def note(i: int, **signal) -> None:
    """Record what the framework tells the call about itself (e.g. its attempt
    number), so the analysis can ask whether a re-execution was visible."""
    with open(os.path.join(os.environ["CS_DIR"], "notes.jsonl"), "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"t": time.time(), "pid": os.getpid(), "i": i, **signal}) + "\n")


def think() -> None:
    """Local work between calls: parsing a reply, deciding the next step."""
    t = float(os.environ.get("CS_THINK", "0"))
    if t:
        time.sleep(t)


def calls() -> int:
    return int(os.environ.get("CS_CALLS", "6"))


def keyed() -> bool:
    return os.environ.get("CS_KEYED") == "1"
