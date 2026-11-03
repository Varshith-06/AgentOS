"""The one call every framework makes: blocking, stdlib, optionally keyed."""

from __future__ import annotations

import glob
import hashlib
import json
import os
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

# Temporal's start-to-close timeout (s): sized to the stand-in provider's call,
# raised for real completions, whose latency has a long tail.
ACTIVITY_TIMEOUT = float(os.environ.get("CS_ACTIVITY_TIMEOUT", "3"))


# A number unique to this process, so a prompt built from it differs in each life.
_NONCE = str(uuid.uuid4().int % 10**6).zfill(6)


def read_lines(paths) -> list[dict]:
    """Records from append-only logs. A kill can cut a process's last line short."""
    out = []
    for path in paths:
        try:
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    try:
                        out.append(json.loads(line))
                    except ValueError:
                        pass
        except OSError:
            pass
    return out


def _replies() -> list[dict]:
    return read_lines(glob.glob(os.path.join(os.environ["CS_DIR"], "replies.*.jsonl")))


def question(i: int) -> str:
    """What call `i` asks. Normally the same in every life. CS_DRIFT changes that,
    as a prompt holding a timestamp or fresh outside state would: `all` differs
    in every life, `tail` only from the first call without a reply on record."""
    mode = os.environ.get("CS_DRIFT")
    if not mode:
        return str(i)
    if mode == "tail":
        for r in _replies():
            if r["i"] == i:
                return r["asked"]
    return f"{i}000{_NONCE}"


def check(i: int, asked: str, reply: dict) -> None:
    """Did this reply answer this question? The stand-in echoes the question and
    a real model is asked to say it; a duplicate count sees neither."""
    if "q" in reply:
        ok = reply["q"] == asked
    elif reply.get("text") and task():
        ok = f"S{i}" in reply["text"]
    elif reply.get("text") and os.environ.get("CS_CHAIN") != "1":
        ok = asked in reply["text"].replace(",", "")
    else:
        ok = None
    # A file per process: agents side by side would overwrite each other's appends.
    path = os.path.join(os.environ["CS_DIR"], f"replies.{os.getpid()}.jsonl")
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"t": time.time(), "pid": os.getpid(), "i": i, "asked": asked,
                             "ok": ok}) + "\n")


def charge(i: int, key: str | None = None, q: str | None = None) -> dict:
    """One billable call. Think time (local work after the reply) is separate.
    A caller that passes `q` checks the reply itself, where the reply lands."""
    asked = question(i) if q is None else q
    reply = _groq(i, key, asked) if os.environ.get("CS_PROVIDER") == "groq" else _standin(i, key, asked)
    if q is None:
        check(i, asked, reply)
    return reply


def _standin(i: int, key: str | None, q: str) -> dict:
    body = json.dumps({"run": os.environ["CS_RUN"], "i": i, "q": q}).encode()
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Idempotency-Key"] = key
    req = urllib.request.Request(
        f"http://{os.environ.get('CS_HOST', '127.0.0.1')}:{os.environ['CS_PORT']}/charge", data=body,
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
# Beware: this is the process making the call. A courier is a new process per
# call, so under couriers every prompt said "(nothing yet)", as the hashes in
# results/v2/real/allam/chain-courier show, and the prompt is built after the
# fingerprint is taken. CS_TASK replaces this: there the agent builds the prompt.
_last: str | None = None


def task() -> bool:
    return os.environ.get("CS_TASK") == "1"


_TASK_FILES = ["agentos/kernel/states.py", "agentos/kernel/messages.py",
               "agentos/kernel/scheduler.py", "agentos/kernel/permissions.py",
               "agentos/kernel/process.py", "agentos/kernel/store.py",
               "agentos/runtime/courier.py", "agentos/api/gateway.py"]


def task_prompt(i: int, notes: list[str]) -> str:
    """One step of a real task, built by the agent: a code review, one file a step.
    Reading the file is the tool. The context grows with the agent's own notes, so
    the prompt depends on every reply before it and is the same in each life only
    if those replies are."""
    path = _TASK_FILES[i % len(_TASK_FILES)]
    source = (Path(__file__).resolve().parents[2] / path).read_text(encoding="utf-8")[:1600]
    earlier = "\n".join(notes) or "(none yet)"
    return (f"You are reviewing a Python codebase one file at a time.\nYour notes so far:\n"
            f"{earlier}\n\nNext file, {path}:\n{source}\n\nBegin your reply with the tag S{i}: "
            "and then add one or two sentences of review notes on this file.")


def _prompt(i: int, q: str) -> str:
    if task():
        return q
    if os.environ.get("CS_CHAIN") == "1":
        return (f"Step {i} of a task. The previous step said: {_last or '(nothing yet)'} "
                "In one short sentence, say what to do next.")
    words = os.environ.get("CS_WORDS")
    if words:  # a call as long as an agent's: seconds, not a fraction of one
        return (f"Begin your reply with the number {q}, then write about {words} words on "
                "how a town library might plan its next year.")
    return f"Reply with the number {q} and nothing else."


def _groq(i: int, key: str | None, q: str) -> dict:
    """The same call against a real model. Groq accepts the header and ignores it."""
    global _last
    model = os.environ.get("CS_MODEL", "openai/gpt-oss-120b")
    prompt = _prompt(i, q)
    request = {"model": model, "max_tokens": int(os.environ.get("CS_MAX_TOKENS", "64")),
               "messages": [{"role": "user", "content": prompt}]}
    if os.environ.get("CS_REASONING"):
        request["reasoning_effort"] = os.environ["CS_REASONING"]
    elif model.startswith(("openai/gpt-oss", "qwen/")):  # reasoning models; others reject it
        request["reasoning_effort"] = "low"
    body = json.dumps(request).encode()
    # Groq's edge refuses urllib's default User-Agent with a 403.
    headers = {"Content-Type": "application/json", "User-Agent": "crashstudy/1.0",
               "Authorization": f"Bearer {os.environ[os.environ.get('CS_KEY_VAR', 'GROQ_API_KEY')]}"}
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
        _mark("reply", i, usage=reply.get("usage"))
        _last = (reply["choices"][0]["message"].get("content") or "").strip()
        return {"i": i, "id": reply.get("id"), "text": _last}
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
