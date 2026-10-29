"""Couriers: one process per call, outside the runtime's crash.

A model call's only side effect is the bill, and a tool call has had its effect
by the time it returns. What a runtime crash loses is the reply: the process
waiting for it dies, so recovery cannot tell whether the call ran and must make
it again. A courier is a process of its own that makes one call and writes the
reply beside the journal. A runtime killed mid-call finds the reply, or the
courier still at work, when it recovers, and takes the reply instead of calling
again. No idempotency key, and nothing asked of the provider.

    python -m agentos.runtime.courier      (request as JSON on stdin)

Each call is one row in couriers.db, under its intent's id:

    dispatched -> running -> done | error
         `-> abandoned       recovery got there first: never made

A courier takes `<key>.lock` before it claims its row and holds it until it
exits, so a row still `running` with the lock free is a courier that died
mid-call. That, and only that, is a call nobody can account for.

A courier outlives the runtime's process tree, but not its machine or
container: on Windows it breaks away from the runtime's job object (the job
must allow it), elsewhere it starts its own session. AGENTOS_COURIER_DETACH=0
keeps couriers inside, to die with the runtime.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import json
import os
import sqlite3
import sys
import time
import traceback
import urllib.request  # noqa: F401  most calls are HTTP: loaded before one arrives
from pathlib import Path
from typing import Any, Callable

SCHEMA = """
CREATE TABLE IF NOT EXISTS calls (
    key      TEXT    PRIMARY KEY,   -- '<intent id>.<attempt>'
    intent   INTEGER NOT NULL,
    target   TEXT    NOT NULL,      -- 'module:function'
    meta     TEXT,                  -- what the caller needs to read the reply
    state    TEXT    NOT NULL,
    pid      INTEGER,
    result   TEXT,
    error    TEXT,
    created  REAL    NOT NULL,
    finished REAL
);
CREATE INDEX IF NOT EXISTS calls_by_intent ON calls (intent);
"""

POLL = 0.02  # seconds between looks at a courier someone else started

CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_BREAKAWAY_FROM_JOB = 0x01000000
CREATE_NO_WINDOW = 0x08000000


class CourierError(Exception):
    """The call ran and failed, or never ran: its outcome is known."""


class CourierLost(Exception):
    """The courier died mid-call: the call may or may not have taken effect."""


def _connect(path: Path | str) -> sqlite3.Connection:
    db = sqlite3.connect(path, isolation_level=None, timeout=30.0)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    # As in the store: a killed process cannot lose a committed row.
    db.execute("PRAGMA synchronous=NORMAL")
    db.executescript(SCHEMA)
    return db


def _lock(path: Path) -> int | None:
    """Take the lock without waiting. The fd holds it until closed or exit."""
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


def _unlock(fd: int) -> None:
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    os.close(fd)


class Couriers:
    """The runtime's side: dispatch couriers, wait for them, and after a crash
    say what became of the call each one carried."""

    def __init__(self, dirpath: Path | str, log: Callable[[str], None] | None = None):
        root = Path(dirpath)
        self.dir = root / "couriers"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = root / "couriers.db"
        self.db = _connect(self.path)
        self._log = log or (lambda message: None)
        self.detach = os.environ.get("AGENTOS_COURIER_DETACH", "1") != "0"
        self._contained = False  # said once: they cannot leave this job
        # A courier started ahead of its call, so no call waits for an
        # interpreter to start. Idle, it holds nothing: not even the database.
        self._spare: Any = None

    def reset(self) -> None:
        """A fresh runtime: no call of an earlier one is anyone's to adopt."""
        self.db.execute("DELETE FROM calls")
        for f in self.dir.iterdir():
            try:
                f.unlink()
            except OSError:
                pass  # a courier of the old runtime still holds it

    def release(self) -> None:
        """Let the spare courier go: on EOF it exits without doing anything."""
        spare, self._spare = self._spare, None
        if spare is not None and spare.returncode is None:
            try:
                spare.stdin.close()
            except (OSError, RuntimeError):
                pass  # its loop is gone; so, then, is its pipe

    def close(self) -> None:
        self.release()
        self.db.close()

    def row(self, key: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM calls WHERE key = ?", (key,)).fetchone()

    def latest(self, intent: int) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM calls WHERE intent = ? ORDER BY created DESC, rowid DESC",
            (intent,),
        ).fetchone()

    def meta(self, key: str) -> dict[str, Any]:
        row = self.row(key)
        return json.loads(row["meta"]) if row and row["meta"] else {}

    def alive(self, key: str) -> bool:
        """Is a courier still holding this call?"""
        fd = _lock(self.dir / f"{key}.lock")
        if fd is None:
            return True
        _unlock(fd)
        return False

    async def call(
        self, intent: int, attempt: int, target: str, args: dict[str, Any],
        meta: dict[str, Any] | None = None,
    ) -> Any:
        """Make one call through a courier of its own and return its reply."""
        key = f"{intent}.{attempt}"
        # On record before the courier exists: recovery that finds no row knows
        # the call was never made.
        self.db.execute(
            "INSERT INTO calls (key, intent, target, meta, state, created)"
            " VALUES (?, ?, ?, ?, 'dispatched', ?)",
            (key, intent, target, json.dumps(meta) if meta else None, time.time()),
        )
        child = await self._spawn(key, target, args)
        return await self.wait(key, child)

    async def wait(self, key: str, child: Any = None) -> Any:
        """The reply, once the courier has it.

        Raises CourierError if the call ran and failed or never ran, and
        CourierLost if the courier died mid-call.
        """
        if child is not None:
            await child.wait()
        while True:
            row = self.row(key)
            state = row["state"]
            if state == "done":
                return json.loads(row["result"])
            if state == "error":
                raise CourierError(row["error"])
            if state == "abandoned" or (state == "dispatched" and child is not None):
                raise CourierError(
                    f"courier {key} exited before making the call: {self._stderr(key)}"
                )
            if state == "running" and not self.alive(key):
                if self.row(key)["state"] == "running":  # not just finishing
                    raise CourierLost(
                        f"the courier carrying call {key} died mid-call; it may or "
                        "may not have taken effect"
                    )
                continue
            await asyncio.sleep(POLL)

    def fate(self, intent: int) -> tuple[str, sqlite3.Row | None]:
        """What became of an intent's call, by its courier's account. One of
        unsent (never made), done, failed, running (still being made), lost."""
        row = self.latest(intent)
        if row is None:
            return "unsent", None
        if row["state"] == "dispatched":
            # Too slow to claim it, or dead before it could: either way it never
            # will now, and the call was never made.
            gone = self.db.execute(
                "UPDATE calls SET state = 'abandoned', finished = ?"
                " WHERE key = ? AND state = 'dispatched'",
                (time.time(), row["key"]),
            )
            if gone.rowcount:
                return "unsent", row
            row = self.row(row["key"])
        state = row["state"]
        if state == "abandoned":
            return "unsent", row
        if state == "done":
            return "done", row
        if state == "error":
            return "failed", row
        if self.alive(row["key"]):
            return "running", row
        row = self.row(row["key"])
        if row["state"] != "running":  # it finished between the two looks
            return self.fate(intent)
        return "lost", row

    def _stderr(self, key: str) -> str:
        try:
            text = (self.dir / f"{key}.err").read_text(encoding="utf-8", errors="replace")
        except OSError:
            return "no stderr"
        return text.strip()[-400:] or "no stderr"

    async def _spawn(self, key: str, target: str, args: dict[str, Any]) -> Any:
        request = json.dumps(
            {"db": str(self.path), "dir": str(self.dir), "key": key,
             "target": target, "args": args}
        ).encode("utf-8")
        child, self._spare = self._spare, None
        for _ in range(2):
            if child is None or child.returncode is not None:
                child = await self._start()
            try:
                child.stdin.write(request)
                await child.stdin.drain()
                child.stdin.close()
                break
            except OSError:  # the spare died idle; start one now
                child = None
        else:
            raise CourierError(f"no courier would take call {key}")
        # The next call's courier starts while this one works.
        try:
            self._spare = await self._start()
        except OSError:
            self._spare = None
        return child

    async def _start(self) -> Any:
        flags, kw = 0, {}
        if os.name == "nt":
            flags = CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
            if self.detach:
                flags |= CREATE_BREAKAWAY_FROM_JOB
        elif self.detach:
            kw["start_new_session"] = True
        argv = [sys.executable, "-X", "utf8", "-m", "agentos.runtime.courier"]
        pipes = dict(stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL,
                     stderr=asyncio.subprocess.DEVNULL)
        try:
            return await asyncio.create_subprocess_exec(
                *argv, creationflags=flags, **pipes, **kw)
        except PermissionError:
            # The job this runtime runs in forbids breakaway.
            if not flags & CREATE_BREAKAWAY_FROM_JOB:
                raise
            if not self._contained:
                self._contained = True
                self._log(
                    "couriers cannot leave this runtime's job object; "
                    "they will die with it"
                )
            return await asyncio.create_subprocess_exec(
                *argv, creationflags=flags & ~CREATE_BREAKAWAY_FROM_JOB, **pipes, **kw)


def _resolve(target: str) -> Callable[..., Any]:
    module, _, name = target.partition(":")
    return getattr(importlib.import_module(module), name)


def main() -> int:
    """The courier: claim the call, make it, write the reply, exit."""
    raw = sys.stdin.buffer.read()
    if not raw:
        return 0  # a spare whose runtime let it go, or died
    req = json.loads(raw)
    key = req["key"]
    sys.stderr = open(Path(req["dir"]) / f"{key}.err", "w", encoding="utf-8")
    db = _connect(req["db"])
    lock = _lock(Path(req["dir"]) / f"{key}.lock")
    if lock is None:
        return 1  # another courier holds this call
    claimed = db.execute(
        "UPDATE calls SET state = 'running', pid = ? WHERE key = ? AND state = 'dispatched'",
        (os.getpid(), key),
    )
    if not claimed.rowcount:
        return 0  # recovery abandoned it first: it is not ours to make
    try:
        value = _resolve(req["target"])(**req["args"])
        if inspect.iscoroutine(value):
            value = asyncio.run(value)
        result = json.dumps(value)
    except BaseException as exc:
        traceback.print_exc()
        db.execute(
            "UPDATE calls SET state = 'error', error = ?, finished = ? WHERE key = ?",
            (f"{type(exc).__name__}: {exc}", time.time(), key),
        )
        return 0
    db.execute(
        "UPDATE calls SET state = 'done', result = ?, finished = ? WHERE key = ?",
        (result, time.time(), key),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
