"""Run the suite with the test files in parallel: python tests/run.py

One interpreter per file, capped at the CPU count. Each file already builds
its own Store in its own temporary directory and binds to port 0. The exit
code is the number of failing files.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
RAN = re.compile(r"Ran (\d+) tests? in ([\d.]+)s")
WHICH = re.compile(r"^(?:FAIL|ERROR): (\w+)", re.M)
WHY = re.compile(r"^(\w*(?:Error|Exception|Failure)):?(.*)$", re.M)


def annotate(failed: list) -> None:
    """Put the failure on the run page, not only in the log."""
    for name, _, _, _, output in failed:
        tests = WHICH.findall(output)
        why = WHY.findall(output)
        which = ", ".join(dict.fromkeys(tests)) or "test name not found"
        reason = f"{why[-1][0]}{why[-1][1]}".strip() if why else "reason not found"
        # An annotation is one line; newlines would truncate it silently.
        print(f"::error title={name}::{which} -- {reason}"[:900])

    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("## Failing test files\n\n")
        for name, _, _, _, output in failed:
            fh.write(f"<details><summary><code>{name}</code></summary>\n\n"
                     f"```\n{output.strip()[-4000:]}\n```\n\n</details>\n\n")


def run_file(path: Path) -> tuple[str, bool, int, float, str]:
    """(name, ok, tests, seconds, output) for one test file."""
    t0 = time.perf_counter()
    proc = subprocess.run(
        [sys.executable, "-X", "utf8", "-m", "unittest", f"tests.{path.stem}"],
        cwd=HERE.parent,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    wall = time.perf_counter() - t0
    output = (proc.stdout or "") + (proc.stderr or "")
    match = RAN.search(output)
    tests = int(match.group(1)) if match else 0
    return path.stem, proc.returncode == 0, tests, wall, output


def main() -> int:
    files = sorted(HERE.glob("test_*.py"))
    workers = min(len(files), os.cpu_count() or 4)
    print(f"agentos tests: {len(files)} files, {workers} at a time\n")

    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(run_file, files))
    wall = time.perf_counter() - t0

    failed = [r for r in results if not r[1]]
    for name, ok, tests, secs, output in sorted(results, key=lambda r: -r[3]):
        print(f"  {'ok' if ok else 'FAIL':<6} {name:<24} {tests:>4} tests  {secs:6.1f}s")
    for name, _, _, _, output in failed:
        print(f"\n{'=' * 70}\n{name}\n{'=' * 70}\n{output}")

    if failed and os.environ.get("GITHUB_ACTIONS"):
        annotate(failed)

    total = sum(r[2] for r in results)
    print(f"\nRan {total} tests in {wall:.1f}s "
          f"({len(failed)} of {len(files)} files failed)"
          if failed else
          f"\nRan {total} tests in {wall:.1f}s\n\nOK")
    return len(failed)


if __name__ == "__main__":
    raise SystemExit(main())
