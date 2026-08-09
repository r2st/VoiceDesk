"""Analytics rollups and dashboard reporting (design doc §4.4).

Two things make this area easy to get subtly wrong. The first is the calendar:
a "day" is an IST calendar day, so the UTC window that backs it is offset by
5h30m and a call placed at 23:30 IST belongs to the day the caller thinks it
does, not to the next UTC one. The second is that the rollup is derived data —
it must be safe to recompute, because the nightly worker re-runs a trailing
window and an operator can trigger it by hand.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.analytics import DailyAnalytics
from app.models.business import Business
from app.models.call import CallLog
from app.models.enums import (
    AgentStatus,
    CallDirection,
    CallResolution,
    CallStatus,
    Language,
    Sentiment,
    TelephonyProvider,
)
from app.models.voice_agent import VoiceAgent
from app.services import analytics_service

IST = ZoneInfo("Asia/Kolkata")


def ist_today() -> date:
    return datetime.now(IST).date()


def at_ist(day: date, hour: int = 12, minute: int = 0) -> datetime:
    """A UTC instant that falls at the given IST wall-clock time on ``day``."""
    return datetime.combine(day, time(hour, minute), tzinfo=IST).astimezone(UTC)


def days_ago(n: int, *, hour: int = 12) -> datetime:
    return at_ist(ist_today() - timedelta(days=n), hour)


async def add_call(
    session: AsyncSession,
    business: Business,
    *,
    when: datetime,
    agent: VoiceAgent | None = None,
    direction: CallDirection = CallDirection.INBOUND,
    status: CallStatus = CallStatus.COMPLETED,
    resolution: CallResolution = CallResolution.PENDING,
    answered: bool = True,
    duration_sec: int = 0,
    billable_minutes: int = 0,
    cost_paise: int = 0,
    language: Language | None = None,
    primary_intent: str | None = None,
    sentiment: Sentiment | None = None,
    sentiment_score: float | None = None,
    avg_confidence: float | None = None,
    deleted: bool = False,
) -> CallLog:
    row = CallLog(
        business_id=business.id,
        agent_id=agent.id if agent else None,
        direction=direction,
        status=status,
        caller_number="+919999900000",
        callee_number="+918000000001",
        provider=TelephonyProvider.MOCK,
        provider_call_id=f"mock-{uuid.uuid4().hex[:12]}",
        created_at=when,
        started_at=when,
        answered_at=when if answered else None,
        ended_at=when + timedelta(seconds=duration_sec),
        duration_sec=duration_sec,
        billable_minutes=billable_minutes,
        cost_paise=cost_paise,
        language=language,
        primary_intent=primary_intent,
        sentiment=sentiment,
        sentiment_score=sentiment_score,
        avg_confidence=avg_confidence,
        resolution=resolution,
        deleted_at=when if deleted else None,
    )
    session.add(row)
    await session.flush()
    return row


# --------------------------------------------------------------------------- #
# Day boundaries
# --------------------------------------------------------------------------- #
class TestDayBounds:
    def test_a_day_starts_at_midnight_ist_not_midnight_utc(self):
        start, end = analytics_service.day_bounds(date(2026, 8, 5))

        assert start == datetime(2026, 8, 4, 18, 30, tzinfo=UTC)
        assert end == datetime(2026, 8, 5, 18, 30, tzinfo=UTC)

    def test_the_window_is_half_open_so_days_do_not_overlap(self):
        _, first_end = analytics_service.day_bounds(date(2026, 8, 5))
        second_start, _ = analytics_service.day_bounds(date(2026, 8, 6))

        assert first_end == second_start


# --------------------------------------------------------------------------- #
# rollup_day
# --------------------------------------------------------------------------- #
class TestRollupDay:
    async def test_aggregates_volume_duration_and_money(
        self, session: AsyncSession, business: Business
    ):
        day = date(2026, 8, 5)
        await add_call(
            session,
            business,
            when=at_ist(day),
            duration_sec=120,
            billable_minutes=2,
            cost_paise=300,
        )
        await add_call(
            session,
            business,
            when=at_ist(day, 18),
            duration_sec=60,
            billable_minutes=1,
            cost_paise=150,
            direction=CallDirection.OUTBOUND,
        )

        row = await analytics_service.rollup_day(session, business.id, day)

        assert row.total_calls == 2
        assert row.inbound_calls == 1
        assert row.outbound_calls == 1
        assert row.answered_calls == 2
        assert row.total_duration_sec == 180
        assert row.avg_duration_sec == 90.0
        assert row.total_billable_minutes == 3
        assert row.total_cost_paise == 450

    async def test_counts_resolutions_and_the_resolution_rate(
        self, session: AsyncSession, business: Business
    ):
        day = date(2026, 8, 5)
        for resolution in (
            CallResolution.RESOLVED,
            CallResolution.RESOLVED,
            CallResolution.ESCALATED,
            CallResolution.HANDED_OFF,
        ):
            await add_call(session, business, when=at_ist(day), resolution=resolution)

        row = await analytics_service.rollup_day(session, business.id, day)

        assert row.resolved_calls == 2
        assert row.escalated_calls == 1
        assert row.handed_off_calls == 1
        assert row.resolution_rate == 0.5

    async def test_counts_sentiment_and_averages_confidence(
        self, session: AsyncSession, business: Business
    ):
        day = date(2026, 8, 5)
        await add_call(
            session, business, when=at_ist(day), sentiment=Sentiment.POSITIVE, avg_confidence=0.9
        )
        await add_call(
            session, business, when=at_ist(day), sentiment=Sentiment.NEUTRAL, avg_confidence=0.7
        )
        await add_call(session, business, when=at_ist(day), sentiment=Sentiment.NEGATIVE)

        row = await analytics_service.rollup_day(session, business.id, day)

        assert (row.positive_sentiment, row.neutral_sentiment, row.negative_sentiment) == (1, 1, 1)
        # The call with no confidence score is left out of the mean rather than
        # dragged to zero.
        assert row.avg_confidence == 0.8

    async def test_failed_busy_and_unanswered_calls_are_one_bucket(
        self, session: AsyncSession, business: Business
    ):
        day = date(2026, 8, 5)
        for status in (CallStatus.FAILED, CallStatus.NO_ANSWER, CallStatus.BUSY):
            await add_call(session, business, when=at_ist(day), status=status, answered=False)
        await add_call(session, business, when=at_ist(day), status=CallStatus.COMPLETED)

        row = await analytics_service.rollup_day(session, business.id, day)

        assert row.failed_calls == 3
        assert row.answered_calls == 1
        assert row.total_calls == 4

    async def test_breaks_down_languages_and_intents(
        self, session: AsyncSession, business: Business
    ):
        day = date(2026, 8, 5)
        await add_call(
            session,
            business,
            when=at_ist(day),
            language=Language.HINDI,
            primary_intent="book_appointment",
        )
        await add_call(
            session,
            business,
            when=at_ist(day),
            language=Language.HINDI,
            primary_intent="book_appointment",
        )
        await add_call(
            session,
            business,
            when=at_ist(day),
            language=Language.ENGLISH,
            primary_intent="check_status",
        )
        await add_call(session, business, when=at_ist(day))

        row = await analytics_service.rollup_day(session, business.id, day)

        assert row.language_breakdown == {"hi": 2, "en": 1}
        assert row.intent_breakdown == {"book_appointment": 2, "check_status": 1}

    async def test_an_empty_day_rolls_up_to_zeroes_without_dividing_by_zero(
        self, session: AsyncSession, business: Business
    ):
        row = await analytics_service.rollup_day(session, business.id, date(2026, 8, 5))

        assert row.total_calls == 0
        assert row.avg_duration_sec == 0.0
        assert row.resolution_rate == 0.0
        assert row.avg_confidence == 0.0
        assert row.language_breakdown == {}

    async def test_a_call_late_on_an_ist_evening_belongs_to_that_ist_day(
        self, session: AsyncSession, business: Business
    ):
        day = date(2026, 8, 5)
        # 23:30 IST on the 5th is 18:00 UTC on the 5th — a naive UTC-day rollup
        # would still catch this one.
        await add_call(session, business, when=at_ist(day, 23, 30))
        # 00:30 IST on the 6th is 19:00 UTC on the 5th — a naive UTC-day rollup
        # would wrongly file it under the 5th.
        await add_call(session, business, when=at_ist(date(2026, 8, 6), 0, 30))

        fifth = await analytics_service.rollup_day(session, business.id, day)
        sixth = await analytics_service.rollup_day(session, business.id, date(2026, 8, 6))

        assert fifth.total_calls == 1
        assert sixth.total_calls == 1

    async def test_soft_deleted_calls_are_not_counted(
        self, session: AsyncSession, business: Business
    ):
        day = date(2026, 8, 5)
        await add_call(session, business, when=at_ist(day))
        await add_call(session, business, when=at_ist(day), deleted=True)

        row = await analytics_service.rollup_day(session, business.id, day)

        assert row.total_calls == 1

    async def test_another_tenants_calls_are_not_counted(
        self, session: AsyncSession, business: Business, other_business: Business
    ):
        day = date(2026, 8, 5)
        await add_call(session, business, when=at_ist(day))
        await add_call(session, other_business, when=at_ist(day))
        await add_call(session, other_business, when=at_ist(day))

        assert (await analytics_service.rollup_day(session, business.id, day)).total_calls == 1
        assert (
            await analytics_service.rollup_day(session, other_business.id, day)
        ).total_calls == 2

    async def test_rerunning_updates_the_same_row_rather_than_adding_one(
        self, session: AsyncSession, business: Business
    ):
        day = date(2026, 8, 5)
        await add_call(session, business, when=at_ist(day))
        first = await analytics_service.rollup_day(session, business.id, day)

        await add_call(session, business, when=at_ist(day))
        second = await analytics_service.rollup_day(session, business.id, day)

        assert second.id == first.id
        assert second.total_calls == 2
        assert await session.scalar(select(func.count()).select_from(DailyAnalytics)) == 1

    async def test_recomputes_downwards_when_calls_are_deleted(
        self, session: AsyncSession, business: Business
    ):
        day = date(2026, 8, 5)
        call = await add_call(session, business, when=at_ist(day), duration_sec=60)
        await analytics_service.rollup_day(session, business.id, day)

        call.deleted_at = datetime.now(UTC)
        await session.flush()
        row = await analytics_service.rollup_day(session, business.id, day)

        # Derived data, so a retraction has to propagate — a rollup that only
        # ever grew would keep billing-adjacent numbers permanently inflated.
        assert row.total_calls == 0
        assert row.total_duration_sec == 0

    async def test_a_purged_rollup_row_is_revived_rather_than_duplicated(
        self, session: AsyncSession, business: Business
    ):
        day = date(2026, 8, 5)
        await add_call(session, business, when=at_ist(day))
        first = await analytics_service.rollup_day(session, business.id, day)
        first.deleted_at = datetime.now(UTC)
        await session.flush()

        second = await analytics_service.rollup_day(session, business.id, day)

        assert second.id == first.id
        assert second.deleted_at is None
        assert await session.scalar(select(func.count()).select_from(DailyAnalytics)) == 1

    async def test_an_agent_scoped_rollup_sees_only_that_agents_calls(
        self, session: AsyncSession, business: Business, agent: VoiceAgent
    ):
        day = date(2026, 8, 5)
        other_agent = VoiceAgent(
            business_id=business.id, name="Sales Agent", use_case="lead_qualification"
        )
        session.add(other_agent)
        await session.flush()
        await add_call(session, business, when=at_ist(day), agent=agent)
        await add_call(session, business, when=at_ist(day), agent=other_agent)
        await add_call(session, business, when=at_ist(day))

        scoped = await analytics_service.rollup_day(session, business.id, day, agent_id=agent.id)
        business_wide = await analytics_service.rollup_day(session, business.id, day)

        assert scoped.total_calls == 1
        assert scoped.agent_id == agent.id
        assert business_wide.total_calls == 3
        assert business_wide.agent_id is None

    async def test_the_business_wide_row_cannot_be_duplicated(
        self, session: AsyncSession, business: Business
    ):
        """Two rollup runs racing for one tenant must not both insert.

        ``agent_id`` is nullable, and SQL uniqueness treats NULLs as distinct,
        so a plain ``UNIQUE(business_id, agent_id, date)`` does not cover the
        business-wide row at all.
        """
        day = date(2026, 8, 5)
        await analytics_service.rollup_day(session, business.id, day)
        session.add(DailyAnalytics(business_id=business.id, agent_id=None, date=day))

        with pytest.raises(IntegrityError):
            await session.flush()
        await session.rollback()


# --------------------------------------------------------------------------- #
# rollup_all_agents
# --------------------------------------------------------------------------- #
class TestRollupAllAgents:
    async def test_writes_a_business_row_plus_one_per_agent_with_traffic(
        self, session: AsyncSession, business: Business, agent: VoiceAgent
    ):
        day = date(2026, 8, 5)
        idle_agent = VoiceAgent(business_id=business.id, name="Idle", use_case="faq")
        session.add(idle_agent)
        await session.flush()
        await add_call(session, business, when=at_ist(day), agent=agent)

        rows = await analytics_service.rollup_all_agents(session, business.id, day)

        assert len(rows) == 2
        assert rows[0].agent_id is None
        assert rows[1].agent_id == agent.id
        # An agent that took no calls gets no row — the table stays proportional
        # to traffic, not to the size of the agent roster.
        assert idle_agent.id not in {r.agent_id for r in rows}

    async def test_calls_with_no_agent_only_land_in_the_business_row(
        self, session: AsyncSession, business: Business
    ):
        day = date(2026, 8, 5)
        await add_call(session, business, when=at_ist(day))

        rows = await analytics_service.rollup_all_agents(session, business.id, day)

        assert len(rows) == 1
        assert rows[0].total_calls == 1


# --------------------------------------------------------------------------- #
# dashboard_summary
# --------------------------------------------------------------------------- #
class TestDashboardSummary:
    async def test_reports_the_trailing_window(self, session: AsyncSession, business: Business):
        await add_call(
            session,
            business,
            when=days_ago(0),
            duration_sec=60,
            resolution=CallResolution.RESOLVED,
            billable_minutes=1,
            cost_paise=150,
        )
        await add_call(
            session,
            business,
            when=days_ago(6),
            duration_sec=120,
            direction=CallDirection.OUTBOUND,
            answered=False,
        )
        await add_call(session, business, when=days_ago(30))

        summary = await analytics_service.dashboard_summary(session, business.id, days=7)

        assert summary["period_days"] == 7
        assert summary["total_calls"] == 2
        assert summary["inbound_calls"] == 1
        assert summary["outbound_calls"] == 1
        assert summary["answered_calls"] == 1
        assert summary["answer_rate"] == 0.5
        assert summary["resolution_rate"] == 0.5
        assert summary["total_duration_sec"] == 180
        assert summary["avg_duration_sec"] == 90.0
        assert summary["total_cost_paise"] == 150

    async def test_deltas_compare_against_the_preceding_window(
        self, session: AsyncSession, business: Business
    ):
        for _ in range(4):
            await add_call(session, business, when=days_ago(1))
        for _ in range(2):
            await add_call(session, business, when=days_ago(8))

        summary = await analytics_service.dashboard_summary(session, business.id, days=7)

        assert summary["total_calls"] == 4
        assert summary["deltas"]["total_calls"] == 100.0

    async def test_a_delta_from_an_empty_window_does_not_divide_by_zero(
        self, session: AsyncSession, business: Business
    ):
        await add_call(session, business, when=days_ago(1))

        summary = await analytics_service.dashboard_summary(session, business.id, days=7)

        assert summary["deltas"]["total_calls"] == 100.0

    async def test_two_empty_windows_report_no_change(
        self, session: AsyncSession, business: Business
    ):
        summary = await analytics_service.dashboard_summary(session, business.id, days=7)

        assert summary["total_calls"] == 0
        assert summary["resolution_rate"] == 0.0
        assert summary["deltas"] == {
            "total_calls": 0.0,
            "resolution_rate": 0.0,
            "avg_duration_sec": 0.0,
        }

    async def test_counts_only_active_undeleted_agents(
        self, session: AsyncSession, business: Business, agent: VoiceAgent
    ):
        session.add(
            VoiceAgent(
                business_id=business.id, name="Paused", use_case="faq", status=AgentStatus.PAUSED
            )
        )
        session.add(
            VoiceAgent(
                business_id=business.id,
                name="Gone",
                use_case="faq",
                status=AgentStatus.ACTIVE,
                deleted_at=datetime.now(UTC),
            )
        )
        await session.flush()

        summary = await analytics_service.dashboard_summary(session, business.id)

        assert summary["active_agents"] == 1

    async def test_another_tenants_traffic_is_invisible(
        self, session: AsyncSession, business: Business, other_business: Business
    ):
        await add_call(session, other_business, when=days_ago(1))

        summary = await analytics_service.dashboard_summary(session, business.id, days=7)

        assert summary["total_calls"] == 0


# --------------------------------------------------------------------------- #
# call_timeseries
# --------------------------------------------------------------------------- #
class TestCallTimeseries:
    async def test_fills_days_with_no_rollup_row_with_zeroes(
        self, session: AsyncSession, business: Business
    ):
        day = date(2026, 8, 5)
        await add_call(session, business, when=at_ist(day), duration_sec=60)
        await analytics_service.rollup_day(session, business.id, day)

        series = await analytics_service.call_timeseries(
            session, business.id, date_from=date(2026, 8, 4), date_to=date(2026, 8, 6)
        )

        assert [p["date"] for p in series] == ["2026-08-04", "2026-08-05", "2026-08-06"]
        assert [p["total_calls"] for p in series] == [0, 1, 0]
        # A gap is a real zero, not a missing key the chart has to guess at.
        assert series[0]["avg_duration_sec"] == 0.0

    async def test_a_single_day_range_returns_one_point(
        self, session: AsyncSession, business: Business
    ):
        series = await analytics_service.call_timeseries(
            session, business.id, date_from=date(2026, 8, 5), date_to=date(2026, 8, 5)
        )

        assert len(series) == 1

    async def test_the_agent_series_is_separate_from_the_business_series(
        self, session: AsyncSession, business: Business, agent: VoiceAgent
    ):
        day = date(2026, 8, 5)
        await add_call(session, business, when=at_ist(day), agent=agent)
        await add_call(session, business, when=at_ist(day))
        await analytics_service.rollup_all_agents(session, business.id, day)

        business_wide = await analytics_service.call_timeseries(
            session, business.id, date_from=day, date_to=day
        )
        scoped = await analytics_service.call_timeseries(
            session, business.id, date_from=day, date_to=day, agent_id=agent.id
        )

        assert business_wide[0]["total_calls"] == 2
        assert scoped[0]["total_calls"] == 1

    async def test_soft_deleted_rollup_rows_are_ignored(
        self, session: AsyncSession, business: Business
    ):
        day = date(2026, 8, 5)
        await add_call(session, business, when=at_ist(day))
        row = await analytics_service.rollup_day(session, business.id, day)
        row.deleted_at = datetime.now(UTC)
        await session.flush()

        series = await analytics_service.call_timeseries(
            session, business.id, date_from=day, date_to=day
        )

        assert series[0]["total_calls"] == 0

    async def test_another_tenants_rollups_are_invisible(
        self, session: AsyncSession, business: Business, other_business: Business
    ):
        day = date(2026, 8, 5)
        await add_call(session, other_business, when=at_ist(day))
        await analytics_service.rollup_day(session, other_business.id, day)

        series = await analytics_service.call_timeseries(
            session, business.id, date_from=day, date_to=day
        )

        assert series[0]["total_calls"] == 0


# --------------------------------------------------------------------------- #
# agent_leaderboard
# --------------------------------------------------------------------------- #
class TestAgentLeaderboard:
    async def test_ranks_by_call_volume_and_carries_quality_metrics(
        self, session: AsyncSession, business: Business, agent: VoiceAgent
    ):
        quiet = VoiceAgent(business_id=business.id, name="Quiet Agent", use_case="faq")
        session.add(quiet)
        await session.flush()
        for _ in range(3):
            await add_call(
                session,
                business,
                when=days_ago(1),
                agent=agent,
                duration_sec=60,
                resolution=CallResolution.RESOLVED,
                sentiment_score=0.5,
                billable_minutes=1,
            )
        await add_call(session, business, when=days_ago(1), agent=quiet)

        board = await analytics_service.agent_leaderboard(session, business.id, days=7)

        assert [e["agent_name"] for e in board] == ["Reception Agent", "Quiet Agent"]
        top = board[0]
        assert top["total_calls"] == 3
        assert top["resolved_calls"] == 3
        assert top["resolution_rate"] == 1.0
        assert top["avg_duration_sec"] == 60.0
        assert top["avg_sentiment_score"] == 0.5
        assert top["billable_minutes"] == 3

    async def test_honours_the_limit(
        self, session: AsyncSession, business: Business, agent: VoiceAgent
    ):
        second = VoiceAgent(business_id=business.id, name="Second", use_case="faq")
        session.add(second)
        await session.flush()
        await add_call(session, business, when=days_ago(1), agent=agent)
        await add_call(session, business, when=days_ago(1), agent=second)

        board = await analytics_service.agent_leaderboard(session, business.id, days=7, limit=1)

        assert len(board) == 1

    async def test_calls_with_no_agent_are_excluded(
        self, session: AsyncSession, business: Business
    ):
        await add_call(session, business, when=days_ago(1))

        assert await analytics_service.agent_leaderboard(session, business.id, days=7) == []

    async def test_a_deleted_agents_history_is_still_reported(
        self, session: AsyncSession, business: Business, agent: VoiceAgent
    ):
        await add_call(session, business, when=days_ago(1), agent=agent)
        agent.deleted_at = datetime.now(UTC)
        await session.flush()

        board = await analytics_service.agent_leaderboard(session, business.id, days=7)

        # The calls happened and were billed, so the row stays; only the name
        # is gone.
        assert board[0]["total_calls"] == 1
        assert board[0]["agent_name"] == "Deleted agent"

    async def test_calls_outside_the_window_are_excluded(
        self, session: AsyncSession, business: Business, agent: VoiceAgent
    ):
        await add_call(session, business, when=days_ago(10), agent=agent)

        assert await analytics_service.agent_leaderboard(session, business.id, days=7) == []


# --------------------------------------------------------------------------- #
# Language and intent breakdowns
# --------------------------------------------------------------------------- #
class TestBreakdowns:
    async def test_languages_are_counted_over_the_window(
        self, session: AsyncSession, business: Business
    ):
        await add_call(session, business, when=days_ago(1), language=Language.HINDI)
        await add_call(session, business, when=days_ago(2), language=Language.HINDI)
        await add_call(session, business, when=days_ago(2), language=Language.MARATHI)
        await add_call(session, business, when=days_ago(1))
        await add_call(session, business, when=days_ago(20), language=Language.TAMIL)

        breakdown = await analytics_service.language_breakdown(session, business.id, days=7)

        assert breakdown == {"hi": 2, "mr": 1}

    async def test_intents_are_ranked_and_capped(self, session: AsyncSession, business: Business):
        for _ in range(3):
            await add_call(session, business, when=days_ago(1), primary_intent="book_appointment")
        await add_call(session, business, when=days_ago(1), primary_intent="check_status")

        ranked = await analytics_service.intent_breakdown(session, business.id, days=7)
        capped = await analytics_service.intent_breakdown(session, business.id, days=7, limit=1)

        assert ranked == [
            {"intent": "book_appointment", "count": 3},
            {"intent": "check_status", "count": 1},
        ]
        assert capped == [{"intent": "book_appointment", "count": 3}]

    async def test_an_explicit_window_is_honoured_over_the_trailing_default(
        self, session: AsyncSession, business: Business
    ):
        """The breakdowns must describe the same window as the series.

        ``/analytics/calls`` takes a date range; if the breakdowns silently
        reported the trailing N days from *today* instead, a report for last
        month would pair last month's chart with this month's languages.
        """
        old_day = ist_today() - timedelta(days=40)
        await add_call(
            session,
            business,
            when=at_ist(old_day),
            language=Language.TAMIL,
            primary_intent="check_status",
        )
        await add_call(
            session,
            business,
            when=days_ago(1),
            language=Language.HINDI,
            primary_intent="book_appointment",
        )

        languages = await analytics_service.language_breakdown(
            session, business.id, date_from=old_day, date_to=old_day
        )
        intents = await analytics_service.intent_breakdown(
            session, business.id, date_from=old_day, date_to=old_day
        )

        assert languages == {"ta": 1}
        assert intents == [{"intent": "check_status", "count": 1}]

    async def test_the_breakdowns_can_be_scoped_to_one_agent(
        self, session: AsyncSession, business: Business, agent: VoiceAgent
    ):
        await add_call(
            session,
            business,
            when=days_ago(1),
            agent=agent,
            language=Language.HINDI,
            primary_intent="book_appointment",
        )
        await add_call(
            session,
            business,
            when=days_ago(1),
            language=Language.ENGLISH,
            primary_intent="check_status",
        )

        languages = await analytics_service.language_breakdown(
            session, business.id, days=7, agent_id=agent.id
        )
        intents = await analytics_service.intent_breakdown(
            session, business.id, days=7, agent_id=agent.id
        )

        assert languages == {"hi": 1}
        assert intents == [{"intent": "book_appointment", "count": 1}]

    async def test_soft_deleted_and_cross_tenant_calls_are_excluded(
        self, session: AsyncSession, business: Business, other_business: Business
    ):
        await add_call(session, business, when=days_ago(1), language=Language.HINDI, deleted=True)
        await add_call(session, other_business, when=days_ago(1), language=Language.HINDI)

        assert await analytics_service.language_breakdown(session, business.id, days=7) == {}


# --------------------------------------------------------------------------- #
# HTTP surface
# --------------------------------------------------------------------------- #
class TestAnalyticsApi:
    async def test_dashboard_is_readable_by_a_viewer(
        self, client: AsyncClient, session: AsyncSession, business: Business, viewer_headers
    ):
        await add_call(session, business, when=days_ago(1), resolution=CallResolution.RESOLVED)

        response = await client.get(
            "/api/v1/analytics/dashboard", params={"days": 7}, headers=viewer_headers
        )

        assert response.status_code == 200
        body = response.json()
        assert body["total_calls"] == 1
        assert body["period_days"] == 7
        assert body["from"] < body["to"]
        assert "deltas" in body

    async def test_dashboard_requires_authentication(self, client: AsyncClient):
        assert (await client.get("/api/v1/analytics/dashboard")).status_code == 401

    async def test_call_analytics_returns_series_totals_and_breakdowns(
        self, client: AsyncClient, session: AsyncSession, business: Business, owner_headers
    ):
        day = ist_today() - timedelta(days=1)
        await add_call(
            session,
            business,
            when=at_ist(day),
            duration_sec=60,
            language=Language.HINDI,
            primary_intent="book_appointment",
            resolution=CallResolution.RESOLVED,
            billable_minutes=1,
            cost_paise=150,
        )
        await analytics_service.rollup_day(session, business.id, day)

        response = await client.get(
            "/api/v1/analytics/calls",
            params={"date_from": day.isoformat(), "date_to": day.isoformat()},
            headers=owner_headers,
        )

        assert response.status_code == 200
        body = response.json()
        assert len(body["series"]) == 1
        assert body["totals"]["total_calls"] == 1
        assert body["totals"]["total_duration_sec"] == 60
        assert body["totals"]["resolved_calls"] == 1
        assert body["totals"]["resolution_rate"] == 1.0
        assert body["languages"] == {"hi": 1}
        assert body["intents"] == [{"intent": "book_appointment", "count": 1}]

    async def test_call_analytics_breakdowns_match_the_requested_window(
        self, client: AsyncClient, session: AsyncSession, business: Business, owner_headers
    ):
        old_day = ist_today() - timedelta(days=40)
        await add_call(session, business, when=at_ist(old_day), language=Language.TAMIL)
        await add_call(session, business, when=days_ago(1), language=Language.HINDI)

        response = await client.get(
            "/api/v1/analytics/calls",
            params={"date_from": old_day.isoformat(), "date_to": old_day.isoformat()},
            headers=owner_headers,
        )

        assert response.json()["languages"] == {"ta": 1}

    async def test_call_analytics_defaults_to_the_last_thirty_days(
        self, client: AsyncClient, owner_headers
    ):
        response = await client.get("/api/v1/analytics/calls", headers=owner_headers)

        body = response.json()
        assert len(body["series"]) == 30
        assert body["date_to"] == ist_today().isoformat()

    async def test_an_inverted_range_is_rejected(self, client: AsyncClient, owner_headers):
        response = await client.get(
            "/api/v1/analytics/calls",
            params={"date_from": "2026-08-10", "date_to": "2026-08-01"},
            headers=owner_headers,
        )

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "validation_error"

    async def test_an_oversized_range_is_rejected(self, client: AsyncClient, owner_headers):
        response = await client.get(
            "/api/v1/analytics/calls",
            params={"date_from": "2024-01-01", "date_to": "2026-01-01"},
            headers=owner_headers,
        )

        assert response.status_code == 422
        assert "366" in response.json()["error"]["message"]

    async def test_the_leaderboard_endpoint(
        self,
        client: AsyncClient,
        session: AsyncSession,
        business: Business,
        agent: VoiceAgent,
        owner_headers,
    ):
        await add_call(session, business, when=days_ago(1), agent=agent)

        response = await client.get(
            "/api/v1/analytics/agents", params={"days": 7}, headers=owner_headers
        )

        assert response.status_code == 200
        assert response.json()[0]["agent_id"] == str(agent.id)

    async def test_the_language_and_intent_endpoints(
        self, client: AsyncClient, session: AsyncSession, business: Business, owner_headers
    ):
        await add_call(
            session,
            business,
            when=days_ago(1),
            language=Language.HINDI,
            primary_intent="book_appointment",
        )

        languages = await client.get("/api/v1/analytics/languages", headers=owner_headers)
        intents = await client.get("/api/v1/analytics/intents", headers=owner_headers)

        assert languages.json() == {"hi": 1}
        assert intents.json() == [{"intent": "book_appointment", "count": 1}]

    async def test_rollup_can_be_triggered_by_an_admin(
        self,
        client: AsyncClient,
        session: AsyncSession,
        business: Business,
        agent: VoiceAgent,
        owner_headers,
    ):
        day = ist_today() - timedelta(days=1)
        await add_call(session, business, when=at_ist(day), agent=agent)

        response = await client.post(
            "/api/v1/analytics/rollup", params={"day": day.isoformat()}, headers=owner_headers
        )

        assert response.status_code == 200
        assert response.json()["agents_rolled_up"] == 1
        assert response.json()["date"] == day.isoformat()

    async def test_rollup_defaults_to_yesterday(self, client: AsyncClient, owner_headers):
        response = await client.post("/api/v1/analytics/rollup", headers=owner_headers)

        assert response.json()["date"] == (ist_today() - timedelta(days=1)).isoformat()

    async def test_a_viewer_cannot_trigger_a_rollup(self, client: AsyncClient, viewer_headers):
        response = await client.post("/api/v1/analytics/rollup", headers=viewer_headers)

        assert response.status_code == 403

    async def test_analytics_never_cross_tenants(
        self,
        client: AsyncClient,
        session: AsyncSession,
        business: Business,
        other_headers,
    ):
        await add_call(session, business, when=days_ago(1), language=Language.HINDI)

        dashboard = await client.get("/api/v1/analytics/dashboard", headers=other_headers)
        languages = await client.get("/api/v1/analytics/languages", headers=other_headers)

        assert dashboard.json()["total_calls"] == 0
        assert languages.json() == {}
