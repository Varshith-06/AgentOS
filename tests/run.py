"""Run the suite with the test files in parallel: python tests/run.py

`python -m unittest discover tests` runs everything in one interpreter, one
file after another. Most of the suite's wall time is not assertions, it is
agents — every integration test starts real OS processes and waits on real
scheduling — so the files spend most of their time blocked, and they were
written independent by construction: each one builds its own Store in its own
temporary directory and binds its daemons to port 0. Nothing stops them
running at once except the runner.

This runner starts one interpreter per test file, capped at the CPU count,
and aggregates. Output per file is buffered and printed whole, so failures
read exactly as they would have serially. The exit code is the number of
failing files.
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

    total = sum(r[2] for r in results)
    print(f"\nRan {total} tests in {wall:.1f}s "
          f"({len(failed)} of {len(files)} files failed)"
          if failed else
          f"\nRan {total} tests in {wall:.1f}s\n\nOK")
    return len(failed)


if __name__ == "__main__":
    raise SystemExit(main())
