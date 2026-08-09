"""Redis advisory locks, so scheduled work runs once across API replicas.

The scheduler is embedded in every worker process; without a shared lock, a
three-replica deployment would roll up analytics three times and race on the
same rows. These locks are advisory and best-effort — they serialise jobs that
are already idempotent rather than providing correctness on their own.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from app.core.logging import get_logger
from app.core.redis import get_redis

logger = get_logger(__name__)

LOCK_PREFIX = "voicedesk:lock:"

#: Releasing must be conditional on still owning the lock. If a job overran its
#: TTL the lock may already have been re-acquired by another replica, and an
#: unconditional ``DEL`` would evict that holder mid-run.
RELEASE_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
end
return 0
"""


def _key(name: str) -> str:
    return f"{LOCK_PREFIX}{name}"


async def acquire(name: str, *, ttl_seconds: float) -> str | None:
    """Take the lock, returning an ownership token, or ``None`` if held.

    The TTL is the backstop for a replica that dies mid-job: the lock expires
    on its own rather than wedging the schedule forever.
    """
    token = uuid.uuid4().hex
    acquired = await get_redis().set(_key(name), token, nx=True, px=int(ttl_seconds * 1000))
    return token if acquired else None


async def release(name: str, token: str) -> bool:
    """Release the lock only if ``token`` still owns it."""
    released = await get_redis().eval(RELEASE_SCRIPT, 1, _key(name), token)
    return bool(released)


@asynccontextmanager
async def guard(name: str, *, ttl_seconds: float) -> AsyncIterator[bool]:
    """Run a block only if this process wins the lock.

    Yields ``False`` rather than raising when another replica holds it — a
    contended scheduled job is a normal outcome, not an error.
    """
    token = await acquire(name, ttl_seconds=ttl_seconds)
    if token is None:
        logger.debug("Lock %s is held elsewhere; skipping", name)
        yield False
        return
    try:
        yield True
    finally:
        await release(name, token)
