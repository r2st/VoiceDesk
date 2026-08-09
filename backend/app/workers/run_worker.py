"""Worker entrypoint: ``python -m app.workers.run_worker``.

Runs the scheduler in its own process so maintenance work never competes with
request handling for the API's event loop.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal

from app.core.logging import configure_logging, get_logger
from app.core.redis import close_redis
from app.db.session import dispose_engine
from app.workers.scheduler import Scheduler

logger = get_logger(__name__)


async def main() -> None:
    configure_logging()
    scheduler = Scheduler()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        # Stop at the end of the current tick rather than cancelling mid-job,
        # so a rolling deploy cannot tear down a half-written rollup.
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, scheduler.stop)

    try:
        await scheduler.run_forever()
    finally:
        await close_redis()
        await dispose_engine()


if __name__ == "__main__":
    asyncio.run(main())
