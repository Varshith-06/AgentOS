"""GPU visibility (AgentOS.pdf p.8: the runtime knows all GPU utilization).

Deliberately small and deliberately honest. AgentOS does not *schedule* GPU
memory — the p.4 "GPU-aware scheduling" idea is listed as future work in the
design doc, and pretending otherwise would be worse than reporting nothing.
What this does is report: if an NVIDIA GPU is present, the shared runtime can
show its utilization alongside agents, cost, and memory, which is what the
p.8 dashboard asks for.

No dependency: `nvidia-smi` ships with the driver, and its absence is the
answer rather than an error. Everything degrades to None on a machine with no
GPU, which is most machines this will run on.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from typing import Any

QUERY = "utilization.gpu,memory.used,memory.total,name"
CACHE_TTL = 2.0  # seconds; nvidia-smi costs ~50ms, the dashboard polls faster

_cache: tuple[float, list[dict[str, Any]] | None] = (0.0, None)


def available() -> bool:
    return shutil.which("nvidia-smi") is not None


def utilization(ttl: float = CACHE_TTL) -> list[dict[str, Any]] | None:
    """Per-GPU utilization, or None when there is no GPU to report on.

    Cached: the kernel loop and the dashboard both ask, and shelling out on
    every tick would cost more than the information is worth.
    """
    global _cache
    now = time.monotonic()
    fresh_until, value = _cache
    if now < fresh_until:
        return value

    result: list[dict[str, Any]] | None = None
    if available():
        try:
            out = subprocess.run(
                ["nvidia-smi", f"--query-gpu={QUERY}",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=5, check=True,
            ).stdout
            result = []
            for i, line in enumerate(l for l in out.splitlines() if l.strip()):
                util, used, total, name = (p.strip() for p in line.split(",", 3))
                result.append({
                    "index": i,
                    "name": name,
                    "utilization": float(util),
                    "memory_used_mb": float(used),
                    "memory_total_mb": float(total),
                })
        except (OSError, subprocess.SubprocessError, ValueError):
            result = None  # a driver that will not answer is the same as none
    _cache = (now + ttl, result)
    return result


def summary() -> dict[str, Any] | None:
    """One line for the dashboard tile, or None when there is no GPU."""
    gpus = utilization()
    if not gpus:
        return None
    return {
        "count": len(gpus),
        "utilization": round(sum(g["utilization"] for g in gpus) / len(gpus), 1),
        "memory_used_mb": round(sum(g["memory_used_mb"] for g in gpus)),
        "memory_total_mb": round(sum(g["memory_total_mb"] for g in gpus)),
        "gpus": gpus,
    }
