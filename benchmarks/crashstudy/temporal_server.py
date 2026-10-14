"""Hold a local Temporal dev server open; print its address, then wait to die."""

import asyncio
import sys

from temporalio.testing import WorkflowEnvironment


async def main() -> None:
    async with await WorkflowEnvironment.start_local() as env:
        print(env.client.service_client.config.target_host, flush=True)
        await asyncio.Event().wait()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
