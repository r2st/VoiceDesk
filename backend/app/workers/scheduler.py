"""A small cron-style scheduler for the background jobs.

Deliberately not Celery. The workload is a handful of idempotent, low-frequency
maintenance jobs, and a broker plus a beat process would be more moving parts
than the work justifies. What is needed instead of a broker is that N replicas
running this loop still perform each job once, which is what the Redis lock and
the shared last-run marker provide.

Schedule state lives in Redis rather than in memory so a restarted or
rescheduled replica picks up where the fleet left off instead of re-running
everything on boot.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import settings
from app.core.locks import guard
from app.core.logging import get_logger
from app.core.redis import get_redis
from app.db.session import get_sessionmaker
from app.workers import jobs
from app.workers.jobs import JobResult

logger = get_logger(__name__)

LAST_RUN_PREFIX = "voicedesk:job:last_run:"
COOLDOWN_PREFIX = "voicedesk:job:cooldown:"

#: How long a failed job waits before the loop retries it. Without this a job
#: that fails deterministically would be retried on every tick.
FAILURE_COOLDOWN_SECONDS = 300

JobFunc = Callable[[AsyncSession], Awaitable[JobResult]]


@dataclass(frozen=True, slots=True)
class ScheduledJob:
    """One job plus when to run it.

    Exactly one of ``interval_seconds`` and ``daily_at`` is set: the first for
    "keep this fresh" jobs, the second for jobs that belong at a quiet hour.
    """

    name: str
    func: JobFunc
    interval_seconds: float | None = None
    daily_at: time | None = None
    #: Must comfortably exceed the job's worst-case runtime, or a second
    #: replica could start the job while this one is still working.
    lock_ttl_seconds: float = 600.0

    def __post_init__(self) -> None:
        if (self.interval_seconds is None) == (self.daily_at is None):
            raise ValueError(f"Job {self.name!r} needs exactly one of interval_seconds/daily_at.")

    def last_occurrence(self, now: datetime) -> datetime:
        """The most recent scheduled firing time at or before ``now``.

        Daily jobs are pinned to IST because they are scheduled around the
        quiet hours of Indian businesses, not around UTC midnight.
        """
        if self.daily_at is None:  # pragma: no cover - guarded by __post_init__
            raise ValueError("last_occurrence is only meaningful for daily jobs.")
        tz = ZoneInfo(settings.trai_timezone)
        local = now.astimezone(tz)
        occurrence = local.replace(
            hour=self.daily_at.hour,
            minute=self.daily_at.minute,
            second=0,
            microsecond=0,
        )
        if occurrence > local:
            occurrence -= timedelta(days=1)
        return occurrence.astimezone(UTC)

    def is_due(self, now: datetime, last_run: datetime | None) -> bool:
        """Whether the job should run, given when it last succeeded.

        A job that has never run is always due — on a fresh deployment the
        jobs are exactly the ones that most need a first pass.
        """
        if last_run is None:
            return True
        if self.daily_at is not None:
            return last_run < self.last_occurrence(now)
        return (now - last_run).total_seconds() >= float(self.interval_seconds or 0)


#: The production schedule. Daily times are IST and sit in the overnight lull
#: so a rollup never competes with peak call traffic.
DEFAULT_JOBS: tuple[ScheduledJob, ...] = (
    ScheduledJob("rollup_analytics", jobs.rollup_analytics, interval_seconds=3600),
    ScheduledJob("refresh_billing_usage", jobs.refresh_billing_usage, interval_seconds=1800),
    ScheduledJob("expire_trials", jobs.expire_trials, daily_at=time(0, 30)),
    ScheduledJob("finalize_billing_month", jobs.finalize_billing_month, daily_at=time(1, 0)),
    ScheduledJob("purge_expired_recordings", jobs.purge_expired_recordings, daily_at=time(2, 30)),
    ScheduledJob("prune_refresh_tokens", jobs.prune_refresh_tokens, daily_at=time(3, 0)),
)


class Scheduler:
    """Runs due jobs, once per fleet, until stopped."""

    def __init__(
        self,
        schedule: tuple[ScheduledJob, ...] | list[ScheduledJob] | None = None,
        *,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        tick_seconds: float = 30.0,
    ) -> None:
        self.schedule = tuple(schedule if schedule is not None else DEFAULT_JOBS)
        self._session_factory = session_factory
        self.tick_seconds = tick_seconds
        self._stop = asyncio.Event()

    # -- schedule state ---------------------------------------------------- #
    async def last_run(self, name: str) -> datetime | None:
        raw = await get_redis().get(f"{LAST_RUN_PREFIX}{name}")
        if not raw:
            return None
        try:
            return datetime.fromisoformat(raw.decode() if isinstance(raw, bytes) else raw)
        except ValueError:
            # A malformed marker should not wedge the schedule; treat it as
            # "never ran" so the job recovers on the next tick.
            logger.warning("Discarding unparseable last-run marker for %s: %r", name, raw)
            return None

    async def _mark_run(self, name: str, when: datetime) -> None:
        await get_redis().set(f"{LAST_RUN_PREFIX}{name}", when.isoformat())

    async def _in_cooldown(self, name: str) -> bool:
        return bool(await get_redis().get(f"{COOLDOWN_PREFIX}{name}"))

    async def _start_cooldown(self, name: str) -> None:
        await get_redis().set(f"{COOLDOWN_PREFIX}{name}", "1", ex=FAILURE_COOLDOWN_SECONDS)

    # -- execution --------------------------------------------------------- #
    def _sessions(self) -> async_sessionmaker[AsyncSession]:
        return self._session_factory or get_sessionmaker()

    async def run_job(self, job: ScheduledJob, *, now: datetime | None = None) -> JobResult | None:
        """Run one job if it is due and uncontended. Returns ``None`` if skipped.

        Failures are swallowed by design: one broken job must not stop the
        others, and the loop that calls this has to survive the night.
        """
        moment = now or datetime.now(UTC)

        if not job.is_due(moment, await self.last_run(job.name)):
            return None
        if await self._in_cooldown(job.name):
            logger.debug("Job %s is in failure cooldown; skipping", job.name)
            return None

        async with guard(f"job:{job.name}", ttl_seconds=job.lock_ttl_seconds) as acquired:
            if not acquired:
                return None
            # Re-check under the lock: another replica may have run the job
            # between the check above and the moment the lock was granted.
            if not job.is_due(moment, await self.last_run(job.name)):
                return None

            try:
                async with self._sessions()() as session:
                    try:
                        result = await job.func(session)
                        await session.commit()
                    except Exception:
                        await session.rollback()
                        raise
            except Exception:
                logger.exception("Scheduled job %s failed", job.name)
                await self._start_cooldown(job.name)
                return None

            await self._mark_run(job.name, moment)
            logger.info("Scheduled job %s completed: %s", job.name, result.detail)
            return result

    async def run_due(self, *, now: datetime | None = None) -> list[JobResult]:
        """One pass over the schedule. This is the unit the loop repeats."""
        moment = now or datetime.now(UTC)
        results = []
        for job in self.schedule:
            result = await self.run_job(job, now=moment)
            if result is not None:
                results.append(result)
        return results

    async def run_forever(self) -> None:
        logger.info(
            "Scheduler started with %s job(s), tick=%ss", len(self.schedule), self.tick_seconds
        )
        while not self._stop.is_set():
            try:
                await self.run_due()
            except Exception:  # pragma: no cover - run_job already isolates failures
                logger.exception("Scheduler tick failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.tick_seconds)
            except TimeoutError:
                continue
        logger.info("Scheduler stopped")

    def stop(self) -> None:
        self._stop.set()
