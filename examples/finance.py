"""Capability-based tool access (AgentOS.pdf p.6-7).

The Finance agent asks the kernel for two capabilities. It holds `sql` in the
permission matrix, so the driver runs its queries. It does not hold `browser`,
so the kernel refuses *before dispatch* — the denial is an audit-log entry the
agent can catch and name, not a stack trace.

Notice what Finance.run() never does: it never imports sqlite3 and never opens
a connection. It says "Need: sql" and the driver owns the rest. That is the
device-driver model — swap the SQL backend and no agent changes.

Run it:      python -m agentos.cli run examples/finance.py
Audit log:   python -m agentos.cli logs        (the denial is in there)
Drivers:     python -m agentos.cli tools

Revoke, re-run — behaviour changes with no code edit (then grant it back):

    python -m agentos.cli revoke Finance sql
    python -m agentos.cli run examples/finance.py
    python -m agentos.cli grant Finance sql
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from agentos import Agent, Kernel, KernelError, Permissions

PERMISSIONS = Path(".agentos/permissions.json")


class Finance(Agent):
    async def run(self, ctx):
        await ctx.request_tool("sql", "execute", statement="DROP TABLE IF EXISTS invoices")
        await ctx.request_tool(
            "sql", "execute", statement="CREATE TABLE invoices (client TEXT, amount REAL)"
        )
        for client, amount in [("acme", 1200.0), ("globex", 800.5), ("initech", 99.5)]:
            await ctx.request_tool(
                "sql",
                "execute",
                statement="INSERT INTO invoices VALUES (?, ?)",
                params=[client, amount],
            )
        rows = await ctx.request_tool(
            "sql", "query", query="SELECT COUNT(*) AS n, SUM(amount) AS total FROM invoices"
        )
        await ctx.log(f"{rows[0]['n']} invoices, total {rows[0]['total']}")

        try:
            await ctx.request_tool("browser", "open", url="https://example.com/rates")
            browser = "allowed"
        except KernelError as exc:
            await ctx.log(f"browser refused: {exc}")
            browser = "denied"

        return {"total": rows[0]["total"], "browser": browser}


async def main(slots: int = 4, policy: str = "fifo") -> int:
    if not PERMISSIONS.exists():
        # First run: seed the p.7 matrix. After that the file is the truth —
        # edit it (or use agent grant/revoke) and re-run; no code changes.
        Permissions(path=PERMISSIONS).grant("Finance", "sql")

    kernel = Kernel(
        policy=policy, slots=slots, tools={"sql": {"db": ".agentos/finance.db"}}
    )
    result = await kernel.run_until_done(Finance())
    print(f"\nFinance returned: {result}")
    print("the denial is in the audit log: python -m agentos.cli logs")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
