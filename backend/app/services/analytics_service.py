"""Analytics rollups and dashboard reporting (design doc §4.4)."""

from __future__ import annotations

import uuid
from collections import Counter
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import get_logger
from app.core.tenancy import tenant_select
from app.models.analytics import DailyAnalytics
from app.models.call import CallLog
from app.models.enums import CallDirection, CallResolution, CallStatus, Sentiment
from app.models.voice_agent import VoiceAgent

logger = get_logger(__name__)


def day_bounds(day: date) -> tuple[datetime, datetime]:
    """UTC half-open bounds of an IST calendar day."""
    tz = ZoneInfo(settings.trai_timezone)
    start = datetime(day.year, day.month, day.day, tzinfo=tz)
    return start.astimezone(UTC), (start + timedelta(days=1)).astimezone(UTC)


async def rollup_day(
    session: AsyncSession,
    business_id: uuid.UUID,
    day: date,
    *,
    agent_id: uuid.UUID | None = None,
) -> DailyAnalytics:
    """Aggregate one day of calls into an ``analytics`` row.

    Recomputed from ``call_logs`` each time it runs, so re-running the job for a
    day is safe and self-correcting.
    """
    start, end = day_bounds(day)

    stmt = select(CallLog).where(
        CallLog.business_id == business_id,
        CallLog.deleted_at.is_(None),
        CallLog.created_at >= start,
        CallLog.created_at < end,
    )
    if agent_id is not None:
        stmt = stmt.where(CallLog.agent_id == agent_id)
    calls = list((await session.execute(stmt)).scalars().all())

    row = (
        await session.execute(
            tenant_select(DailyAnalytics, business_id, include_deleted=True).where(
                DailyAnalytics.date == day,
                DailyAnalytics.agent_id == agent_id
                if agent_id is not None
                else DailyAnalytics.agent_id.is_(None),
            )
        )
    ).scalar_one_or_none()

    if row is None:
        row = DailyAnalytics(business_id=business_id, agent_id=agent_id, date=day)
        session.add(row)

    total = len(calls)
    answered = [c for c in calls if c.answered_at is not None]
    durations = [c.duration_sec for c in calls if c.duration_sec]
    confidences = [c.avg_confidence for c in calls if c.avg_confidence is not None]

    resolved = sum(1 for c in calls if c.resolution == CallResolution.RESOLVED)
    languages: Counter[str] = Counter(c.language for c in calls if c.language)
    intents: Counter[str] = Counter(c.primary_intent for c in calls if c.primary_intent)

    row.total_calls = total
    row.inbound_calls = sum(1 for c in calls if c.direction == CallDirection.INBOUND)
    row.outbound_calls = sum(1 for c in calls if c.direction == CallDirection.OUTBOUND)
    row.answered_calls = len(answered)
    row.failed_calls = sum(
        1
        for c in calls
        if c.status in (CallStatus.FAILED, CallStatus.NO_ANSWER, CallStatus.BUSY)
    )
    row.total_duration_sec = sum(durations)
    row.avg_duration_sec = round(sum(durations) / len(durations), 2) if durations else 0.0
    row.total_billable_minutes = sum(c.billable_minutes for c in calls)
    row.total_cost_paise = sum(c.cost_paise for c in calls)
    row.resolved_calls = resolved
    row.escalated_calls = sum(1 for c in calls if c.resolution == CallResolution.ESCALATED)
    row.handed_off_calls = sum(1 for c in calls if c.resolution == CallResolution.HANDED_OFF)
    row.resolution_rate = round(resolved / total, 4) if total else 0.0
    row.positive_sentiment = sum(1 for c in calls if c.sentiment == Sentiment.POSITIVE)
    row.neutral_sentiment = sum(1 for c in calls if c.sentiment == Sentiment.NEUTRAL)
    row.negative_sentiment = sum(1 for c in calls if c.sentiment == Sentiment.NEGATIVE)
    row.avg_confidence = (
        round(sum(confidences) / len(confidences), 4) if confidences else 0.0
    )
    row.language_breakdown = dict(languages)
    row.intent_breakdown = dict(intents)
    row.deleted_at = None

    await session.flush()
    return row


async def rollup_all_agents(
    session: AsyncSession, business_id: uuid.UUID, day: date
) -> list[DailyAnalytics]:
    """Business-wide row plus one per agent that had traffic that day."""
    rows = [await rollup_day(session, business_id, day)]
    agent_ids = (
        (
            await session.execute(
                select(CallLog.agent_id)
                .where(
                    CallLog.business_id == business_id,
                    CallLog.deleted_at.is_(None),
                    CallLog.agent_id.is_not(None),
                    CallLog.created_at >= day_bounds(day)[0],
                    CallLog.created_at < day_bounds(day)[1],
                )
                .distinct()
            )
        )
        .scalars()
        .all()
    )
    for agent_id in agent_ids:
        rows.append(await rollup_day(session, business_id, day, agent_id=agent_id))
    return rows


async def dashboard_summary(
    session: AsyncSession, business_id: uuid.UUID, *, days: int = 30
) -> dict:
    """Headline metrics for the dashboard home screen."""
    tz = ZoneInfo(settings.trai_timezone)
    today = datetime.now(tz).date()
    start, _ = day_bounds(today - timedelta(days=days - 1))
    _, end = day_bounds(today)
    prev_start, _ = day_bounds(today - timedelta(days=days * 2 - 1))
    _, prev_end = day_bounds(today - timedelta(days=days))

    current = await _window_metrics(session, business_id, start, end)
    previous = await _window_metrics(session, business_id, prev_start, prev_end)

    active_agents = await session.scalar(
        select(func.count())
        .select_from(VoiceAgent)
        .where(
            VoiceAgent.business_id == business_id,
            VoiceAgent.deleted_at.is_(None),
            VoiceAgent.status == "active",
        )
    )

    return {
        "period_days": days,
        "from": start.isoformat(),
        "to": end.isoformat(),
        "active_agents": int(active_agents or 0),
        **current,
        "deltas": {
            "total_calls": _pct_change(current["total_calls"], previous["total_calls"]),
            "resolution_rate": round(
                current["resolution_rate"] - previous["resolution_rate"], 4
            ),
            "avg_duration_sec": _pct_change(
                current["avg_duration_sec"], previous["avg_duration_sec"]
            ),
        },
    }


async def _window_metrics(
    session: AsyncSession, business_id: uuid.UUID, start: datetime, end: datetime
) -> dict:
    row = (
        await session.execute(
            select(
                func.count(CallLog.id),
                func.coalesce(func.sum(CallLog.duration_sec), 0),
                func.coalesce(func.avg(CallLog.duration_sec), 0.0),
                func.coalesce(func.sum(CallLog.billable_minutes), 0),
                func.coalesce(func.sum(CallLog.cost_paise), 0),
                func.sum(case((CallLog.resolution == CallResolution.RESOLVED, 1), else_=0)),
                func.sum(case((CallLog.direction == CallDirection.INBOUND, 1), else_=0)),
                func.sum(case((CallLog.answered_at.is_not(None), 1), else_=0)),
                func.sum(case((CallLog.sentiment == Sentiment.POSITIVE, 1), else_=0)),
                func.sum(case((CallLog.sentiment == Sentiment.NEGATIVE, 1), else_=0)),
                func.sum(case((CallLog.resolution == CallResolution.HANDED_OFF, 1), else_=0)),
            ).where(
                CallLog.business_id == business_id,
                CallLog.deleted_at.is_(None),
                CallLog.created_at >= start,
                CallLog.created_at < end,
            )
        )
    ).one()

    total = int(row[0] or 0)
    resolved = int(row[5] or 0)
    return {
        "total_calls": total,
        "total_duration_sec": int(row[1] or 0),
        "avg_duration_sec": round(float(row[2] or 0.0), 2),
        "total_billable_minutes": int(row[3] or 0),
        "total_cost_paise": int(row[4] or 0),
        "resolved_calls": resolved,
        "resolution_rate": round(resolved / total, 4) if total else 0.0,
        "inbound_calls": int(row[6] or 0),
        "outbound_calls": total - int(row[6] or 0),
        "answered_calls": int(row[7] or 0),
        "answer_rate": round(int(row[7] or 0) / total, 4) if total else 0.0,
        "positive_sentiment": int(row[8] or 0),
        "negative_sentiment": int(row[9] or 0),
        "handed_off_calls": int(row[10] or 0),
    }


def _pct_change(current: float, previous: float) -> float:
    if not previous:
        return 100.0 if current else 0.0
    return round((current - previous) / previous * 100, 2)


async def call_timeseries(
    session: AsyncSession,
    business_id: uuid.UUID,
    *,
    date_from: date,
    date_to: date,
    agent_id: uuid.UUID | None = None,
) -> list[dict]:
    """Daily series from the rollup table, filling gaps with zeroes."""
    stmt = tenant_select(DailyAnalytics, business_id).where(
        DailyAnalytics.date >= date_from,
        DailyAnalytics.date <= date_to,
        DailyAnalytics.agent_id == agent_id
        if agent_id is not None
        else DailyAnalytics.agent_id.is_(None),
    )
    rows = {
        row.date: row
        for row in (await session.execute(stmt.order_by(DailyAnalytics.date))).scalars().all()
    }

    series: list[dict] = []
    cursor = date_from
    while cursor <= date_to:
        row = rows.get(cursor)
        series.append(
            {
                "date": cursor.isoformat(),
                "total_calls": row.total_calls if row else 0,
                "inbound_calls": row.inbound_calls if row else 0,
                "outbound_calls": row.outbound_calls if row else 0,
                "answered_calls": row.answered_calls if row else 0,
                "avg_duration_sec": row.avg_duration_sec if row else 0.0,
                "resolution_rate": row.resolution_rate if row else 0.0,
                "billable_minutes": row.total_billable_minutes if row else 0,
                "cost_paise": row.total_cost_paise if row else 0,
                "positive_sentiment": row.positive_sentiment if row else 0,
                "negative_sentiment": row.negative_sentiment if row else 0,
            }
        )
        cursor += timedelta(days=1)
    return series


async def agent_leaderboard(
    session: AsyncSession, business_id: uuid.UUID, *, days: int = 30, limit: int = 10
) -> list[dict]:
    """Rank agents by call volume with their quality metrics."""
    tz = ZoneInfo(settings.trai_timezone)
    today = datetime.now(tz).date()
    start, _ = day_bounds(today - timedelta(days=days - 1))
    _, end = day_bounds(today)

    rows = (
        await session.execute(
            select(
                CallLog.agent_id,
                func.count(CallLog.id),
                func.coalesce(func.avg(CallLog.duration_sec), 0.0),
                func.sum(case((CallLog.resolution == CallResolution.RESOLVED, 1), else_=0)),
                func.coalesce(func.avg(CallLog.sentiment_score), 0.0),
                func.coalesce(func.sum(CallLog.billable_minutes), 0),
            )
            .where(
                CallLog.business_id == business_id,
                CallLog.deleted_at.is_(None),
                CallLog.agent_id.is_not(None),
                CallLog.created_at >= start,
                CallLog.created_at < end,
            )
            .group_by(CallLog.agent_id)
            .order_by(func.count(CallLog.id).desc())
            .limit(limit)
        )
    ).all()

    names = {
        agent.id: agent.name
        for agent in (
            await session.execute(tenant_select(VoiceAgent, business_id))
        ).scalars().all()
    }

    leaderboard = []
    for agent_id, total, avg_duration, resolved, avg_sentiment, minutes in rows:
        total = int(total or 0)
        leaderboard.append(
            {
                "agent_id": str(agent_id),
                "agent_name": names.get(agent_id, "Deleted agent"),
                "total_calls": total,
                "avg_duration_sec": round(float(avg_duration or 0.0), 2),
                "resolved_calls": int(resolved or 0),
                "resolution_rate": round(int(resolved or 0) / total, 4) if total else 0.0,
                "avg_sentiment_score": round(float(avg_sentiment or 0.0), 3),
                "billable_minutes": int(minutes or 0),
            }
        )
    return leaderboard


async def language_breakdown(
    session: AsyncSession, business_id: uuid.UUID, *, days: int = 30
) -> dict[str, int]:
    tz = ZoneInfo(settings.trai_timezone)
    today = datetime.now(tz).date()
    start, _ = day_bounds(today - timedelta(days=days - 1))
    _, end = day_bounds(today)

    rows = (
        await session.execute(
            select(CallLog.language, func.count(CallLog.id))
            .where(
                CallLog.business_id == business_id,
                CallLog.deleted_at.is_(None),
                CallLog.language.is_not(None),
                CallLog.created_at >= start,
                CallLog.created_at < end,
            )
            .group_by(CallLog.language)
        )
    ).all()
    return {language: int(count) for language, count in rows}


async def intent_breakdown(
    session: AsyncSession, business_id: uuid.UUID, *, days: int = 30, limit: int = 20
) -> list[dict]:
    tz = ZoneInfo(settings.trai_timezone)
    today = datetime.now(tz).date()
    start, _ = day_bounds(today - timedelta(days=days - 1))
    _, end = day_bounds(today)

    rows = (
        await session.execute(
            select(CallLog.primary_intent, func.count(CallLog.id))
            .where(
                CallLog.business_id == business_id,
                CallLog.deleted_at.is_(None),
                CallLog.primary_intent.is_not(None),
                CallLog.created_at >= start,
                CallLog.created_at < end,
            )
            .group_by(CallLog.primary_intent)
            .order_by(func.count(CallLog.id).desc())
            .limit(limit)
        )
    ).all()
    return [{"intent": intent, "count": int(count)} for intent, count in rows]
