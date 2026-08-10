"""Failed-login throttling.

The global rate limiter allows 100 requests a minute per IP, which is roughly
144,000 password guesses a day from one address — and far more from a botnet.
These tests pin the tighter, failure-only budget that sits in front of the
sign-in path.

The two counters answer different attacks and are tested separately:
the per-account counter stops a distributed attack on one inbox, and the
per-IP counter stops one host spraying a common password across many accounts.
Neither alone is sufficient.
"""

from __future__ import annotations

import pytest

from app.core import throttle
from app.core.throttle import (
    MAX_ATTEMPTS_PER_ACCOUNT,
    MAX_ATTEMPTS_PER_IP,
    WINDOW_SECONDS,
    LoginThrottleError,
)

EMAIL = "owner@sunrisediagnostics.in"
IP = "203.0.113.9"


async def fail_times(count: int, *, email: str = EMAIL, ip: str | None = IP) -> None:
    for _ in range(count):
        await throttle.record_failure(email, ip)


class TestAccountBudget:
    async def test_an_untouched_account_is_allowed(self):
        assert (await throttle.check(EMAIL, IP)).allowed

    async def test_attempts_below_the_cap_stay_allowed(self):
        await fail_times(MAX_ATTEMPTS_PER_ACCOUNT - 1)
        assert (await throttle.check(EMAIL, IP)).allowed

    async def test_the_account_locks_at_the_cap(self):
        await fail_times(MAX_ATTEMPTS_PER_ACCOUNT)
        decision = await throttle.check(EMAIL, IP)
        assert not decision.allowed
        assert decision.scope == "account"

    async def test_enforce_raises_once_the_account_is_locked(self):
        await fail_times(MAX_ATTEMPTS_PER_ACCOUNT)
        with pytest.raises(LoginThrottleError) as excinfo:
            await throttle.enforce(EMAIL, IP)
        assert excinfo.value.status_code == 429
        assert excinfo.value.code == "too_many_attempts"

    async def test_the_lockout_message_does_not_reveal_whether_the_account_exists(self):
        await fail_times(MAX_ATTEMPTS_PER_ACCOUNT)
        with pytest.raises(LoginThrottleError) as excinfo:
            await throttle.enforce(EMAIL, IP)
        message = excinfo.value.message.lower()
        assert "exist" not in message and "unknown" not in message
        assert EMAIL not in excinfo.value.message

    async def test_enforce_is_silent_while_under_budget(self):
        await fail_times(MAX_ATTEMPTS_PER_ACCOUNT - 1)
        await throttle.enforce(EMAIL, IP)  # must not raise

    async def test_the_counter_is_case_insensitive(self):
        """A lockout that 'Owner@…' sidesteps by shifting case is no lockout."""
        await fail_times(MAX_ATTEMPTS_PER_ACCOUNT, email=EMAIL.upper())
        assert not (await throttle.check(EMAIL.lower(), IP)).allowed

    async def test_surrounding_whitespace_does_not_open_a_second_budget(self):
        await fail_times(MAX_ATTEMPTS_PER_ACCOUNT, email=f"  {EMAIL}  ")
        assert not (await throttle.check(EMAIL, IP)).allowed

    async def test_accounts_are_counted_independently(self):
        await fail_times(MAX_ATTEMPTS_PER_ACCOUNT)
        assert (await throttle.check("someone-else@sunrisediagnostics.in", None)).allowed

    async def test_a_successful_sign_in_clears_the_account(self):
        """Mistyping a password three times must not strand a legitimate user."""
        await fail_times(MAX_ATTEMPTS_PER_ACCOUNT - 1)
        await throttle.clear(EMAIL)
        assert (await throttle.check(EMAIL, IP)).allowed

    async def test_clearing_an_account_that_never_failed_is_harmless(self):
        await throttle.clear("never-seen@sunrisediagnostics.in")

    async def test_the_lockout_expires_with_the_window(self, fake_redis):
        await fail_times(MAX_ATTEMPTS_PER_ACCOUNT)
        assert not (await throttle.check(EMAIL, IP)).allowed
        fake_redis.advance(WINDOW_SECONDS + 1)
        assert (await throttle.check(EMAIL, IP)).allowed

    async def test_the_window_is_fixed_from_the_first_failure(self, fake_redis):
        """A fresh TTL per attempt would let an attacker hold the owner out forever."""
        await fail_times(1)
        fake_redis.advance(WINDOW_SECONDS - 10)
        await fail_times(MAX_ATTEMPTS_PER_ACCOUNT)
        fake_redis.advance(20)  # past the original first-failure deadline
        assert (await throttle.check(EMAIL, IP)).allowed


class TestAddressBudget:
    async def test_one_address_is_capped_across_many_accounts(self):
        """Credential stuffing never trips a per-account counter; this catches it."""
        for index in range(MAX_ATTEMPTS_PER_IP):
            await throttle.record_failure(f"victim{index}@sunrisediagnostics.in", IP)

        decision = await throttle.check("fresh@sunrisediagnostics.in", IP)
        assert not decision.allowed
        assert decision.scope == "ip"

    async def test_a_different_address_is_unaffected(self):
        for index in range(MAX_ATTEMPTS_PER_IP):
            await throttle.record_failure(f"victim{index}@sunrisediagnostics.in", IP)
        assert (await throttle.check("fresh@sunrisediagnostics.in", "198.51.100.4")).allowed

    async def test_the_address_budget_is_looser_than_the_account_budget(self):
        assert MAX_ATTEMPTS_PER_IP > MAX_ATTEMPTS_PER_ACCOUNT

    async def test_failures_without_an_address_only_count_against_the_account(self):
        await fail_times(MAX_ATTEMPTS_PER_IP, email="anon@sunrisediagnostics.in", ip=None)
        assert (await throttle.check("other@sunrisediagnostics.in", IP)).allowed

    async def test_the_account_budget_is_checked_before_the_address_budget(self):
        """The account scope is the more precise diagnosis when both apply."""
        for index in range(MAX_ATTEMPTS_PER_IP):
            await throttle.record_failure(f"victim{index}@sunrisediagnostics.in", IP)
        await fail_times(MAX_ATTEMPTS_PER_ACCOUNT)
        assert (await throttle.check(EMAIL, IP)).scope == "account"


class TestDegradedRedis:
    """A cache outage must not lock every customer out of their own dashboard."""

    async def test_check_fails_open_when_redis_is_unreachable(self, fake_redis, monkeypatch):
        async def broken_get(*_args, **_kwargs):
            raise ConnectionError("redis unreachable")

        monkeypatch.setattr(fake_redis, "get", broken_get)
        assert (await throttle.check(EMAIL, IP)).allowed

    async def test_enforce_fails_open_when_redis_is_unreachable(self, fake_redis, monkeypatch):
        async def broken_get(*_args, **_kwargs):
            raise ConnectionError("redis unreachable")

        monkeypatch.setattr(fake_redis, "get", broken_get)
        await throttle.enforce(EMAIL, IP)  # must not raise

    async def test_recording_a_failure_survives_an_outage(self, fake_redis, monkeypatch):
        async def broken_incr(*_args, **_kwargs):
            raise ConnectionError("redis unreachable")

        monkeypatch.setattr(fake_redis, "incr", broken_incr)
        await throttle.record_failure(EMAIL, IP)  # must not raise

    async def test_clearing_survives_an_outage(self, fake_redis, monkeypatch):
        async def broken_delete(*_args, **_kwargs):
            raise ConnectionError("redis unreachable")

        monkeypatch.setattr(fake_redis, "delete", broken_delete)
        await throttle.clear(EMAIL)  # must not raise

    async def test_a_corrupt_counter_is_treated_as_zero(self, fake_redis):
        """A junk cache value should not lock anyone out."""
        fake_redis.store[f"loginfail:account:{EMAIL}"] = "not-a-number"
        assert (await throttle.check(EMAIL, IP)).allowed
