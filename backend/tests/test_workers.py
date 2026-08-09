"""Locks, the job scheduler and every scheduled job.

The scheduler is exercised through ``run_due``/``run_job`` rather than
``run_forever`` so nothing here waits on wall-clock time; the loop itself is
two lines over ``run_due``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core import locks
from app.models.business import Business, RefreshToken, User
from app.models.call import CallLog, CallRecording
from app.models.enums import (
    BusinessStatus,
    CallDirection,
    CallResolution,
    CallStatus,
    Language,
    PlanTier,
    Sentiment,
    TelephonyProvider,
)
from app.workers import jobs
from app.workers.scheduler import (
    COOLDOWN_PREFIX,
    LAST_RUN_PREFIX,
    ScheduledJob,
    Scheduler,
)

IST = ZoneInfo("Asia/Kolkata")


# --------------------------------------------------------------------------- #
# Redis advisory locks
# --------------------------------------------------------------------------- #
class TestLocks:
    async def test_acquire_returns_a_token(self):
        token = await locks.acquire("rollup", ttl_seconds=60)
        assert token

    async def test_second_acquire_is_refused_while_held(self):
        assert await locks.acquire("rollup", ttl_seconds=60)
        assert await locks.acquire("rollup", ttl_seconds=60) is None

    async def test_release_frees_the_lock(self):
        token = await locks.acquire("rollup", ttl_seconds=60)
        assert token is not None
        assert await locks.release("rollup", token) is True
        assert await locks.acquire("rollup", ttl_seconds=60)

    async def test_release_with_a_foreign_token_is_refused(self):
        """A replica that overran its TTL must not evict the new holder."""
        assert await locks.acquire("rollup", ttl_seconds=60)
        assert await locks.release("rollup", "not-the-token") is False
        # The real owner still holds it.
        assert await locks.acquire("rollup", ttl_seconds=60) is None

    async def test_lock_expires_so_a_dead_replica_cannot_wedge_the_schedule(self, fake_redis):
        assert await locks.acquire("rollup", ttl_seconds=30)
        fake_redis.advance(31)
        assert await locks.acquire("rollup", ttl_seconds=30)

    async def test_guard_yields_true_and_releases_on_exit(self):
        async with locks.guard("rollup", ttl_seconds=60) as acquired:
            assert acquired is True
        assert await locks.acquire("rollup", ttl_seconds=60)

    async def test_guard_yields_false_when_contended(self):
        assert await locks.acquire("rollup", ttl_seconds=60)
        async with locks.guard("rollup", ttl_seconds=60) as acquired:
            assert acquired is False

    async def test_guard_releases_even_when_the_body_raises(self):
        with pytest.raises(RuntimeError):
            async with locks.guard("rollup", ttl_seconds=60):
                raise RuntimeError("boom")
        assert await locks.acquire("rollup", ttl_seconds=60)


# --------------------------------------------------------------------------- #
# Schedule arithmetic
# --------------------------------------------------------------------------- #
class TestScheduledJob:
    async def _noop(self, session: AsyncSession) -> jobs.JobResult:
        return jobs.JobResult("noop")

    def test_a_job_needs_exactly_one_trigger(self):
        with pytest.raises(ValueError):
            ScheduledJob("bad", self._noop)
        with pytest.raises(ValueError):
            ScheduledJob("bad", self._noop, interval_seconds=60, daily_at=time(1, 0))

    def test_a_job_that_never_ran_is_due(self):
        job = ScheduledJob("j", self._noop, interval_seconds=3600)
        assert job.is_due(datetime.now(UTC), None) is True

    def test_interval_job_waits_out_its_interval(self):
        job = ScheduledJob("j", self._noop, interval_seconds=3600)
        now = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)
        assert job.is_due(now, now - timedelta(minutes=59)) is False
        assert job.is_due(now, now - timedelta(minutes=61)) is True

    def test_daily_occurrence_is_pinned_to_ist(self):
        job = ScheduledJob("j", self._noop, daily_at=time(2, 30))
        # 05:00 IST on 8 Aug — the 02:30 IST firing has already passed today.
        now = datetime(2026, 8, 8, 5, 0, tzinfo=IST)
        assert job.last_occurrence(now) == datetime(2026, 8, 8, 2, 30, tzinfo=IST)

    def test_daily_occurrence_rolls_back_a_day_before_the_hour(self):
        job = ScheduledJob("j", self._noop, daily_at=time(2, 30))
        now = datetime(2026, 8, 8, 1, 0, tzinfo=IST)
        assert job.last_occurrence(now) == datetime(2026, 8, 7, 2, 30, tzinfo=IST)

    def test_daily_job_runs_once_per_occurrence(self):
        job = ScheduledJob("j", self._noop, daily_at=time(2, 30))
        now = datetime(2026, 8, 8, 5, 0, tzinfo=IST)
        # Already ran after today's 02:30 firing.
        assert job.is_due(now, datetime(2026, 8, 8, 3, 0, tzinfo=IST)) is False
        # Last ran before it — still owed.
        assert job.is_due(now, datetime(2026, 8, 8, 1, 0, tzinfo=IST)) is True

    def test_daily_job_catches_up_after_an_outage(self):
        """A worker down for two days runs the job once on return, not twice."""
        job = ScheduledJob("j", self._noop, daily_at=time(2, 30))
        now = datetime(2026, 8, 8, 5, 0, tzinfo=IST)
        assert job.is_due(now, datetime(2026, 8, 5, 3, 0, tzinfo=IST)) is True


# --------------------------------------------------------------------------- #
# Scheduler execution
# --------------------------------------------------------------------------- #
@pytest.fixture
def session_factory(engine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)


async def _committed_business(session_factory: async_sessionmaker[AsyncSession]) -> uuid.UUID:
    """Insert a tenant and commit it.

    The shared ``business`` fixture only flushes, and every session in the
    suite rides the same StaticPool connection — so a job that rolls back would
    also unwind the fixture's insert and the assertion would be testing the
    fixture rather than the scheduler. Committing here pins the row down first.
    """
    async with session_factory() as setup:
        row = Business(
            name="Rollback Subject",
            slug=f"rollback-{uuid.uuid4().hex[:8]}",
            phone="+919876500000",
            email="owner@rollback.test",
            plan=PlanTier.STARTER,
            status=BusinessStatus.ACTIVE,
            settings_json={},
        )
        setup.add(row)
        await setup.commit()
        return row.id


class TestScheduler:
    async def test_runs_a_due_job_and_records_the_run(self, session_factory, fake_redis):
        seen: list[str] = []

        async def job_func(session: AsyncSession) -> jobs.JobResult:
            seen.append("ran")
            return jobs.JobResult("counter", {"n": 1})

        scheduler = Scheduler(
            [ScheduledJob("counter", job_func, interval_seconds=60)],
            session_factory=session_factory,
        )
        results = await scheduler.run_due()

        assert seen == ["ran"]
        assert [r.name for r in results] == ["counter"]
        assert await fake_redis.get(f"{LAST_RUN_PREFIX}counter")

    async def test_does_not_rerun_inside_the_interval(self, session_factory):
        runs: list[str] = []

        async def job_func(session: AsyncSession) -> jobs.JobResult:
            runs.append("ran")
            return jobs.JobResult("counter")

        scheduler = Scheduler(
            [ScheduledJob("counter", job_func, interval_seconds=3600)],
            session_factory=session_factory,
        )
        now = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)
        await scheduler.run_due(now=now)
        await scheduler.run_due(now=now + timedelta(minutes=5))

        assert runs == ["ran"]

    async def test_reruns_once_the_interval_has_passed(self, session_factory):
        runs: list[str] = []

        async def job_func(session: AsyncSession) -> jobs.JobResult:
            runs.append("ran")
            return jobs.JobResult("counter")

        scheduler = Scheduler(
            [ScheduledJob("counter", job_func, interval_seconds=3600)],
            session_factory=session_factory,
        )
        now = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)
        await scheduler.run_due(now=now)
        await scheduler.run_due(now=now + timedelta(hours=2))

        assert runs == ["ran", "ran"]

    async def test_a_contended_job_is_skipped(self, session_factory):
        """The second replica must not duplicate work the first is doing."""
        runs: list[str] = []

        async def job_func(session: AsyncSession) -> jobs.JobResult:
            runs.append("ran")
            return jobs.JobResult("counter")

        job = ScheduledJob("counter", job_func, interval_seconds=60)
        scheduler = Scheduler([job], session_factory=session_factory)

        # Another replica already holds the lock.
        assert await locks.acquire("job:counter", ttl_seconds=60)
        assert await scheduler.run_job(job) is None
        assert runs == []

    async def test_a_failing_job_does_not_record_a_run_and_enters_cooldown(
        self, session_factory, fake_redis
    ):
        async def job_func(session: AsyncSession) -> jobs.JobResult:
            raise RuntimeError("provider exploded")

        job = ScheduledJob("flaky", job_func, interval_seconds=60)
        scheduler = Scheduler([job], session_factory=session_factory)

        assert await scheduler.run_job(job) is None
        assert await fake_redis.get(f"{LAST_RUN_PREFIX}flaky") is None
        assert await fake_redis.get(f"{COOLDOWN_PREFIX}flaky")

    async def test_cooldown_suppresses_the_immediate_retry(self, session_factory):
        attempts: list[str] = []

        async def job_func(session: AsyncSession) -> jobs.JobResult:
            attempts.append("tried")
            raise RuntimeError("still broken")

        job = ScheduledJob("flaky", job_func, interval_seconds=1)
        scheduler = Scheduler([job], session_factory=session_factory)

        await scheduler.run_job(job)
        await scheduler.run_job(job)
        assert attempts == ["tried"]

    async def test_the_job_retries_once_the_cooldown_lapses(self, session_factory, fake_redis):
        attempts: list[str] = []

        async def job_func(session: AsyncSession) -> jobs.JobResult:
            attempts.append("tried")
            raise RuntimeError("still broken")

        job = ScheduledJob("flaky", job_func, interval_seconds=1)
        scheduler = Scheduler([job], session_factory=session_factory)

        await scheduler.run_job(job)
        fake_redis.advance(301)
        await scheduler.run_job(job)
        assert attempts == ["tried", "tried"]

    async def test_a_failing_job_releases_its_lock(self, session_factory):
        async def job_func(session: AsyncSession) -> jobs.JobResult:
            raise RuntimeError("boom")

        job = ScheduledJob("flaky", job_func, interval_seconds=60)
        await Scheduler([job], session_factory=session_factory).run_job(job)
        assert await locks.acquire("job:flaky", ttl_seconds=60)

    async def test_one_broken_job_does_not_stop_the_others(self, session_factory):
        healthy: list[str] = []

        async def broken(session: AsyncSession) -> jobs.JobResult:
            raise RuntimeError("boom")

        async def works(session: AsyncSession) -> jobs.JobResult:
            healthy.append("ran")
            return jobs.JobResult("works")

        scheduler = Scheduler(
            [
                ScheduledJob("broken", broken, interval_seconds=60),
                ScheduledJob("works", works, interval_seconds=60),
            ],
            session_factory=session_factory,
        )
        results = await scheduler.run_due()
        assert healthy == ["ran"]
        assert [r.name for r in results] == ["works"]

    async def test_a_corrupt_last_run_marker_is_treated_as_never_run(
        self, session_factory, fake_redis
    ):
        runs: list[str] = []

        async def job_func(session: AsyncSession) -> jobs.JobResult:
            runs.append("ran")
            return jobs.JobResult("counter")

        await fake_redis.set(f"{LAST_RUN_PREFIX}counter", "not-a-timestamp")
        scheduler = Scheduler(
            [ScheduledJob("counter", job_func, interval_seconds=3600)],
            session_factory=session_factory,
        )
        await scheduler.run_due()
        assert runs == ["ran"]

    async def test_work_is_committed(self, session_factory):
        """The job's own session is committed, not left open for a caller."""
        business_id = await _committed_business(session_factory)

        async def job_func(session: AsyncSession) -> jobs.JobResult:
            row = await session.get(Business, business_id)
            assert row is not None
            row.name = "Renamed By Job"
            return jobs.JobResult("rename")

        scheduler = Scheduler(
            [ScheduledJob("rename", job_func, interval_seconds=60)],
            session_factory=session_factory,
        )
        await scheduler.run_due()

        async with session_factory() as verify:
            reloaded = await verify.get(Business, business_id)
            assert reloaded is not None
            assert reloaded.name == "Renamed By Job"

    async def test_a_failed_job_rolls_its_work_back(self, session_factory):
        business_id = await _committed_business(session_factory)

        async def job_func(session: AsyncSession) -> jobs.JobResult:
            row = await session.get(Business, business_id)
            assert row is not None
            row.name = "Half-written"
            await session.flush()
            raise RuntimeError("failed after writing")

        scheduler = Scheduler(
            [ScheduledJob("half", job_func, interval_seconds=60)],
            session_factory=session_factory,
        )
        await scheduler.run_due()

        async with session_factory() as verify:
            reloaded = await verify.get(Business, business_id)
            assert reloaded is not None
            assert reloaded.name != "Half-written"

    def test_the_default_schedule_has_unique_names(self):
        from app.workers.scheduler import DEFAULT_JOBS

        names = [job.name for job in DEFAULT_JOBS]
        assert len(names) == len(set(names))


# --------------------------------------------------------------------------- #
# Jobs
# --------------------------------------------------------------------------- #
def _utc_at(day, hour: int = 12) -> datetime:
    """A UTC instant that lands inside ``day`` when read in IST."""
    return datetime(day.year, day.month, day.day, hour, 0, tzinfo=IST).astimezone(UTC)


@pytest_asyncio.fixture
async def completed_call(session: AsyncSession, business, agent, phone_number) -> CallLog:
    row = CallLog(
        business_id=business.id,
        agent_id=agent.id,
        phone_number_id=phone_number.id,
        direction=CallDirection.INBOUND,
        status=CallStatus.COMPLETED,
        caller_number="+919999900001",
        callee_number=phone_number.number,
        provider=TelephonyProvider.MOCK,
        started_at=_utc_at(jobs.today_ist()),
        answered_at=_utc_at(jobs.today_ist()),
        ended_at=_utc_at(jobs.today_ist()),
        duration_sec=125,
        billable_minutes=3,
        cost_paise=900,
        language=Language.HINDI,
        sentiment=Sentiment.POSITIVE,
        resolution=CallResolution.RESOLVED,
        primary_intent="book_appointment",
        avg_confidence=0.91,
    )
    row.created_at = _utc_at(jobs.today_ist())
    session.add(row)
    await session.flush()
    return row


class TestRollupAnalytics:
    async def test_writes_a_row_for_the_business_and_the_agent(
        self, session, business, agent, completed_call
    ):
        result = await jobs.rollup_analytics(session, day=jobs.today_ist())

        assert result.name == "rollup_analytics"
        # One business-wide row plus one for the agent that had traffic.
        assert result.detail["rows_written"] == 2
        assert result.detail["businesses"] == 1

    async def test_the_rollup_reflects_the_underlying_calls(
        self, session, business, completed_call
    ):
        from app.models.analytics import DailyAnalytics

        await jobs.rollup_analytics(session, day=jobs.today_ist())
        row = (
            await session.execute(
                select(DailyAnalytics).where(
                    DailyAnalytics.business_id == business.id,
                    DailyAnalytics.agent_id.is_(None),
                )
            )
        ).scalar_one()

        assert row.total_calls == 1
        assert row.resolved_calls == 1
        assert row.total_billable_minutes == 3
        assert row.language_breakdown == {"hi": 1}

    async def test_rerunning_the_job_does_not_double_count(self, session, business, completed_call):
        from app.models.analytics import DailyAnalytics

        await jobs.rollup_analytics(session, day=jobs.today_ist())
        await jobs.rollup_analytics(session, day=jobs.today_ist())

        rows = (
            (
                await session.execute(
                    select(DailyAnalytics).where(
                        DailyAnalytics.business_id == business.id,
                        DailyAnalytics.agent_id.is_(None),
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1
        assert rows[0].total_calls == 1

    async def test_the_default_window_covers_today_and_yesterday(self, session, business):
        result = await jobs.rollup_analytics(session)
        assert result.detail["days"] == [
            jobs.today_ist().isoformat(),
            (jobs.today_ist() - timedelta(days=1)).isoformat(),
        ]

    async def test_cancelled_tenants_are_skipped(self, session, business, other_business):
        other_business.status = BusinessStatus.CANCELLED
        await session.flush()

        result = await jobs.rollup_analytics(session, day=jobs.today_ist())
        assert result.detail["businesses"] == 1

    async def test_soft_deleted_tenants_are_skipped(self, session, business, other_business):
        other_business.deleted_at = datetime.now(UTC)
        await session.flush()

        result = await jobs.rollup_analytics(session, day=jobs.today_ist())
        assert result.detail["businesses"] == 1


class TestPurgeExpiredRecordings:
    async def _recording(self, session, business, call, *, expires_at, storage) -> CallRecording:
        key = storage.build_key(business.id, call.id, "opus")
        storage.upload(key, b"audio-bytes")
        row = CallRecording(
            business_id=business.id,
            call_id=call.id,
            storage_path=key,
            storage_bucket=storage.bucket,
            expires_at=expires_at,
        )
        session.add(row)
        await session.flush()
        return row

    async def test_expired_audio_is_deleted(self, session, business, call, fake_storage):
        recording = await self._recording(
            session,
            business,
            call,
            expires_at=datetime.now(UTC) - timedelta(days=1),
            storage=fake_storage,
        )
        result = await jobs.purge_expired_recordings(session)

        assert result.detail["purged"] == 1
        assert recording.storage_path not in fake_storage.objects

    async def test_the_metadata_row_survives_as_an_audit_trail(
        self, session, business, call, fake_storage
    ):
        recording = await self._recording(
            session,
            business,
            call,
            expires_at=datetime.now(UTC) - timedelta(days=1),
            storage=fake_storage,
        )
        await jobs.purge_expired_recordings(session)

        assert recording.purged_at is not None
        assert recording.deleted_at is not None

    async def test_unexpired_audio_is_left_alone(self, session, business, call, fake_storage):
        recording = await self._recording(
            session,
            business,
            call,
            expires_at=datetime.now(UTC) + timedelta(days=30),
            storage=fake_storage,
        )
        result = await jobs.purge_expired_recordings(session)

        assert result.detail["purged"] == 0
        assert recording.storage_path in fake_storage.objects

    async def test_rerunning_the_purge_is_a_no_op(self, session, business, call, fake_storage):
        await self._recording(
            session,
            business,
            call,
            expires_at=datetime.now(UTC) - timedelta(days=1),
            storage=fake_storage,
        )
        assert (await jobs.purge_expired_recordings(session)).detail["purged"] == 1
        assert (await jobs.purge_expired_recordings(session)).detail["purged"] == 0


class TestBillingJobs:
    async def test_usage_is_recomputed_from_the_calls(self, session, business, completed_call):
        result = await jobs.refresh_billing_usage(session)

        assert result.detail["businesses"] == 1
        assert result.detail["minutes_used"] == 3

    async def test_previous_month_wraps_across_the_year(self):
        assert jobs.previous_month("2026-01") == "2025-12"
        assert jobs.previous_month("2026-08") == "2026-07"

    async def test_finalizing_assigns_an_invoice_number(self, session, business):
        from app.models.analytics import BillingUsage

        period = jobs.previous_month()
        result = await jobs.finalize_billing_month(session, month=period)

        assert result.detail["finalized"] == 1
        usage = (
            await session.execute(
                select(BillingUsage).where(
                    BillingUsage.business_id == business.id, BillingUsage.month == period
                )
            )
        ).scalar_one()
        assert usage.is_finalized is True
        assert usage.invoice_number is not None

    async def test_refinalizing_keeps_the_original_invoice_number(self, session, business):
        period = jobs.previous_month()
        await jobs.finalize_billing_month(session, month=period)

        from app.models.analytics import BillingUsage

        usage = (
            await session.execute(
                select(BillingUsage).where(BillingUsage.business_id == business.id)
            )
        ).scalar_one()
        original, finalized_at = usage.invoice_number, usage.finalized_at

        await jobs.finalize_billing_month(session, month=period)
        assert usage.invoice_number == original
        assert usage.finalized_at == finalized_at

    async def test_a_finalized_cycle_is_not_recomputed(self, session, business, completed_call):
        """Late-arriving calls must not silently rewrite an issued invoice."""
        from app.services import billing_service

        period = billing_service.current_month()
        await jobs.finalize_billing_month(session, month=period)

        completed_call.billable_minutes = 999
        await session.flush()
        await jobs.refresh_billing_usage(session, month=period)

        from app.models.analytics import BillingUsage

        usage = (
            await session.execute(
                select(BillingUsage).where(BillingUsage.business_id == business.id)
            )
        ).scalar_one()
        assert usage.minutes_used == 3


class TestExpireTrials:
    async def test_a_lapsed_trial_is_suspended(self, session, business):
        business.status = BusinessStatus.TRIAL
        business.trial_ends_at = datetime.now(UTC) - timedelta(days=1)
        await session.flush()

        result = await jobs.expire_trials(session)

        assert result.detail["suspended"] == 1
        assert business.status == BusinessStatus.SUSPENDED

    async def test_a_running_trial_is_untouched(self, session, business):
        business.status = BusinessStatus.TRIAL
        business.trial_ends_at = datetime.now(UTC) + timedelta(days=5)
        await session.flush()

        result = await jobs.expire_trials(session)

        assert result.detail["suspended"] == 0
        assert business.status == BusinessStatus.TRIAL

    async def test_a_paid_tenant_is_never_suspended_by_a_stale_trial_date(self, session, business):
        """Converting to a paid plan moves the status off ``trial``; the old
        ``trial_ends_at`` must not come back to bite the customer."""
        business.status = BusinessStatus.ACTIVE
        business.plan = PlanTier.GROWTH
        business.trial_ends_at = datetime.now(UTC) - timedelta(days=90)
        await session.flush()

        result = await jobs.expire_trials(session)

        assert result.detail["suspended"] == 0
        assert business.status == BusinessStatus.ACTIVE

    async def test_a_trial_without_an_end_date_is_untouched(self, session, business):
        business.status = BusinessStatus.TRIAL
        business.trial_ends_at = None
        await session.flush()

        assert (await jobs.expire_trials(session)).detail["suspended"] == 0

    async def test_rerunning_suspends_nobody_twice(self, session, business):
        business.status = BusinessStatus.TRIAL
        business.trial_ends_at = datetime.now(UTC) - timedelta(days=1)
        await session.flush()

        assert (await jobs.expire_trials(session)).detail["suspended"] == 1
        assert (await jobs.expire_trials(session)).detail["suspended"] == 0


class TestPruneRefreshTokens:
    async def _token(
        self, session, user: User, *, expires_at: datetime, revoked_at: datetime | None = None
    ) -> RefreshToken:
        row = RefreshToken(
            business_id=user.business_id,
            user_id=user.id,
            token_hash=uuid.uuid4().hex + uuid.uuid4().hex,
            expires_at=expires_at,
            revoked_at=revoked_at,
        )
        session.add(row)
        await session.flush()
        return row

    async def test_long_expired_tokens_are_deleted(self, session, owner):
        await self._token(session, owner, expires_at=datetime.now(UTC) - timedelta(days=30))

        result = await jobs.prune_refresh_tokens(session)

        assert result.detail["deleted"] == 1
        assert (await session.execute(select(RefreshToken))).scalars().all() == []

    async def test_live_tokens_are_kept(self, session, owner):
        await self._token(session, owner, expires_at=datetime.now(UTC) + timedelta(days=7))

        result = await jobs.prune_refresh_tokens(session)

        assert result.detail["deleted"] == 0
        assert len((await session.execute(select(RefreshToken))).scalars().all()) == 1

    async def test_recently_expired_tokens_stay_inside_the_grace_window(self, session, owner):
        """Reuse detection needs a short window after expiry to still fire."""
        await self._token(session, owner, expires_at=datetime.now(UTC) - timedelta(days=1))

        result = await jobs.prune_refresh_tokens(session, grace_days=7)

        assert result.detail["deleted"] == 0

    async def test_long_revoked_tokens_are_deleted_even_if_not_yet_expired(self, session, owner):
        await self._token(
            session,
            owner,
            expires_at=datetime.now(UTC) + timedelta(days=30),
            revoked_at=datetime.now(UTC) - timedelta(days=30),
        )

        assert (await jobs.prune_refresh_tokens(session)).detail["deleted"] == 1
