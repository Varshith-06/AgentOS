"""The main run again, with every call a real completion (Groq).

    GROQ_API_KEY=... CS_ACTIVITY_TIMEOUT=30 python -m benchmarks.crashstudy.realmain \
        --model openai/gpt-oss-20b --trials 10

Every configuration, with and without keys, `--trials` valid crashes each, for
one model. Run one process per model: each model has its own daily request
quota, and counting admissions needs one caller per quota. A crash is valid if
it was killed on time and met no provider error; the rest are drawn again.

The free tier allows about 1000 requests a day per model, refilled one every
86.4 s, so this waits whenever the quota runs low and picks up where it left
off when restarted.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx

from benchmarks.crashstudy.realprovider import API, HERE, ROOT, _headers

CONFIGS = ["agentos", "langgraph-node", "langgraph-loop", "temporal", "dbos",
           "crewai", "crewai-guarded", "crewai-checkpoint", "prefect", "autogen"]
VALID = {"ok", "failed"}
PER_TRIAL = 11      # six calls, duplicates, two probes: requests reserved per crash
CALIBRATION = 14    # two uncrashed runs and their probes


def remaining(model: str) -> tuple[int, float]:
    """Requests left in the model's daily quota, and seconds per refilled one."""
    for _ in range(6):
        r = httpx.post(API, headers=_headers(), timeout=60, json={
            "model": model, "max_tokens": 1, "messages": [{"role": "user", "content": "1"}]})
        if r.status_code == 200:
            return (int(r.headers["x-ratelimit-remaining-requests"]),
                    86400 / int(r.headers["x-ratelimit-limit-requests"]))
        time.sleep(float(r.headers.get("retry-after", 30)) + 1)
    return 0, 86.4


def done(out: Path, fw: str, keyed: bool) -> int:
    if not out.exists():
        return 0
    rows = [json.loads(l) for l in out.read_text(encoding="utf-8").splitlines() if l.strip()]
    return sum(r["framework"] == fw and r["keyed"] == keyed and r["status"] in VALID
               for r in rows)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", required=True)
    ap.add_argument("--trials", type=int, default=10)
    ap.add_argument("--frameworks", default=",".join(CONFIGS))
    ap.add_argument("--keyed", default="no,yes", help="key modes: no, yes, or no,yes")
    ap.add_argument("--calls", type=int, default=6)
    ap.add_argument("--think", type=float, default=0.1)
    ap.add_argument("--chain", action="store_true",
                    help="each prompt carries the previous reply (CS_CHAIN=1)")
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("--gateway", action="store_true",
                    help="send every call through an AgentOS gateway")
    ap.add_argument("--crash", choices=["runtime", "agent"], default="runtime")
    ap.add_argument("--adopt", choices=["yes", "no"], default="yes")
    ap.add_argument("--courier", choices=["off", "on", "contained"], default="off")
    ap.add_argument("--out", help="default: results/real/real-main-<model>.jsonl")
    args = ap.parse_args()
    slug = args.model.split("/")[-1]
    out = Path(args.out) if args.out else HERE / "results" / "real" / f"real-main-{slug}.jsonl"
    modes = [k == "yes" for k in args.keyed.split(",")]
    fws = args.frameworks.split(",")
    # Each model starts at a different configuration, so the streams running
    # side by side are not all inside the same framework at once.
    shift = sum(map(ord, slug)) % len(fws)
    order = fws[shift:] + fws[:shift]
    env = dict(os.environ, CS_CHAIN="1" if args.chain else "0")

    while True:
        todo = [(fw, args.trials - min(done(out, fw, k) for k in modes)) for fw in order]
        todo = [(fw, n) for fw, n in todo if n > 0]
        if not todo:
            break
        fw, n = todo[0]
        need = CALIBRATION + len(modes) * n * (PER_TRIAL + 2 * (args.calls - 6))
        left, per = remaining(args.model)
        if left < need:
            # The next quota probe spends a request too, so wait for two more.
            wait = (need - left + 2) * per
            print(f"{time.strftime('%X')} {slug}: {left} requests left, {fw} needs {need}; "
                  f"waiting {wait / 60:.0f} min", flush=True)
            time.sleep(wait)
            continue
        print(f"{time.strftime('%X')} {slug}: {fw}, {n} per key mode", flush=True)
        subprocess.run([args.python, "-m", "benchmarks.crashstudy.realprovider", "e2e",
                        "--framework", fw, "--model", args.model, "--trials", str(n),
                        "--keyed", args.keyed, "--calibrate", "2",
                        "--calls", str(args.calls), "--think", str(args.think),
                        "--seed", str(time.time_ns() % 2**31),
                        "--python", args.python, "--out", str(out)]
                       + (["--gateway"] if args.gateway else [])
                       + ["--crash", args.crash, "--adopt", args.adopt,
                          "--courier", args.courier], cwd=ROOT, env=env)
    print(f"{time.strftime('%X')} {slug}: all configurations done", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
