"""Failed-login throttling.

The global rate limiter (``app.core.ratelimit``) keys unauthenticated traffic
by IP at 100 requests/minute, which is the right budget for browsing but far
too generous for a password prompt — it permits roughly 144,000 guesses a day
from a single address, and a botnet spreads that across as many addresses as it
likes.

This adds a second, much tighter budget that counts only *failures* and keys
them by the account being targeted as well as by source address:

* **Per account** — caps guesses against one email regardless of where they
  come from, which is the control that actually matters for a distributed
  attack.
* **Per address** — caps credential-stuffing sweeps that try one password
  against many different accounts, which the per-account counter never sees.

A successful sign-in clears the account counter, so a user who mistypes a few
times and then gets it right is never left locked out.

Like the rate limiter, this fails **open**: a Redis outage must not lock every
customer out of their dashboard.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.errors import RateLimitError
from app.core.logging import get_logger
from app.core.redis import get_redis

logger = get_logger(__name__)

#: Failures against a single account before it is locked for the window.
MAX_ATTEMPTS_PER_ACCOUNT = 8
#: Failures from a single address, across all accounts, before it is blocked.
MAX_ATTEMPTS_PER_IP = 25
#: How long a counter lives. Also the lockout duration, since the key expires.
WINDOW_SECONDS = 900  # 15 minutes


class LoginThrottleError(RateLimitError):
    """Too many failed sign-in attempts for this account or address."""

    code = "too_many_attempts"


@dataclass(frozen=True, slots=True)
class ThrottleDecision:
    """Why a sign-in attempt was blocked, for logging and tests."""

    allowed: bool
    scope: str | None = None
    retry_after: int = WINDOW_SECONDS


def _account_key(email: str) -> str:
    return f"loginfail:account:{email.strip().lower()}"


def _ip_key(client_ip: str) -> str:
    return f"loginfail:ip:{client_ip}"


async def _count(key: str) -> int:
    raw = await get_redis().get(key)
    if raw is None:
        return 0
    try:
        return int(raw)
    except (TypeError, ValueError):
        # A key of an unexpected shape is treated as "no failures" rather than
        # locking someone out over a cache inconsistency.
        return 0


async def check(email: str, client_ip: str | None = None) -> ThrottleDecision:
    """Decide whether this sign-in attempt may proceed."""
    try:
        if await _count(_account_key(email)) >= MAX_ATTEMPTS_PER_ACCOUNT:
            return ThrottleDecision(allowed=False, scope="account")
        if client_ip and await _count(_ip_key(client_ip)) >= MAX_ATTEMPTS_PER_IP:
            return ThrottleDecision(allowed=False, scope="ip")
    except Exception as exc:  # pragma: no cover - depends on Redis being down
        logger.warning("Login throttle unavailable, allowing attempt: %s", exc)
        return ThrottleDecision(allowed=True)
    return ThrottleDecision(allowed=True)


async def enforce(email: str, client_ip: str | None = None) -> None:
    """Raise :class:`LoginThrottleError` if this attempt is over budget."""
    decision = await check(email, client_ip)
    if decision.allowed:
        return
    logger.warning("Blocked sign-in attempt: %s budget exhausted", decision.scope)
    # The message deliberately does not say whether the account exists.
    raise LoginThrottleError(
        "Too many failed sign-in attempts. Try again in a few minutes.",
        details={"retry_after_seconds": decision.retry_after},
    )


async def record_failure(email: str, client_ip: str | None = None) -> None:
    """Count a failed attempt against both the account and the source address."""
    keys = [_account_key(email)] + ([_ip_key(client_ip)] if client_ip else [])
    try:
        client = get_redis()
        for key in keys:
            count = await client.incr(key)
            # Only the first failure sets the TTL, so the window is fixed from
            # the first bad password rather than sliding forward with each new
            # attempt — otherwise a persistent attacker would keep it alive
            # indefinitely and never let the legitimate owner back in.
            if count == 1:
                await client.expire(key, WINDOW_SECONDS)
    except Exception as exc:  # pragma: no cover - depends on Redis being down
        logger.warning("Could not record failed sign-in: %s", exc)


async def clear(email: str) -> None:
    """Reset an account's failure count after a successful sign-in."""
    try:
        await get_redis().delete(_account_key(email))
    except Exception as exc:  # pragma: no cover - depends on Redis being down
        logger.warning("Could not clear sign-in failures: %s", exc)
