"""The one call every framework makes: blocking, stdlib, optionally keyed."""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request


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


def _mark(what: str, i: int) -> None:
    # Appended and flushed before and after the call, so a kill leaves a record
    # of which call was outstanding.
    with open(os.path.join(os.environ["CS_DIR"], "calls.jsonl"), "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"t": time.time(), "what": what, "i": i}) + "\n")


def _groq(i: int, key: str | None) -> dict:
    """The same call against a real model. Groq accepts the header and ignores it."""
    body = json.dumps({
        "model": os.environ.get("CS_MODEL", "openai/gpt-oss-120b"),
        "max_tokens": 64, "reasoning_effort": "low",
        "messages": [{"role": "user", "content": f"Reply with the number {i} and nothing else."}],
    }).encode()
    # Groq's edge refuses urllib's default User-Agent with a 403.
    headers = {"Content-Type": "application/json", "User-Agent": "crashstudy/1.0",
               "Authorization": f"Bearer {os.environ['GROQ_API_KEY']}"}
    if key:
        headers["Idempotency-Key"] = key
    for _ in range(5):
        req = urllib.request.Request("https://api.groq.com/openai/v1/chat/completions",
                                     data=body, headers=headers, method="POST")
        _mark("send", i)
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
