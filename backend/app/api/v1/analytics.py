"""``/api/v1/analytics`` — dashboard metrics and call analytics (design doc §4.4)."""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Query

from app.core.config import settings
from app.core.deps import CurrentContext, DbSession, RequireAdmin
from app.core.errors import ValidationError
from app.schemas.analytics import (
    AgentLeaderboardEntry,
    CallAnalyticsOut,
    DashboardSummary,
    IntentCount,
    RollupResult,
    TimeseriesPoint,
    WindowMetrics,
)
from app.services import analytics_service

router = APIRouter(prefix="/analytics", tags=["analytics"])

#: Guard rail on how far back a single query may scan.
MAX_RANGE_DAYS = 366


def _today() -> date:
    return datetime.now(ZoneInfo(settings.trai_timezone)).date()


@router.get("/dashboard", response_model=DashboardSummary)
async def dashboard(
    context: CurrentContext,
    session: DbSession,
    days: Annotated[int, Query(ge=1, le=365)] = 30,
) -> DashboardSummary:
    """Headline metrics with deltas against the preceding window."""
    summary = await analytics_service.dashboard_summary(session, context.business_id, days=days)
    return DashboardSummary.model_validate(summary)


@router.get("/calls", response_model=CallAnalyticsOut)
async def call_analytics(
    context: CurrentContext,
    session: DbSession,
    date_from: Annotated[date | None, Query()] = None,
    date_to: Annotated[date | None, Query()] = None,
    agent_id: Annotated[uuid.UUID | None, Query()] = None,
) -> CallAnalyticsOut:
    """Daily series plus language and intent breakdowns for the window."""
    end = date_to or _today()
    start = date_from or (end - timedelta(days=29))
    if start > end:
        raise ValidationError("'date_from' must not be after 'date_to'.")
    span = (end - start).days + 1
    if span > MAX_RANGE_DAYS:
        raise ValidationError(f"Range is limited to {MAX_RANGE_DAYS} days (asked for {span}).")

    series = await analytics_service.call_timeseries(
        session, context.business_id, date_from=start, date_to=end, agent_id=agent_id
    )
    # The breakdowns take the same window and agent filter as the series, so
    # every number in the payload describes one thing.
    languages = await analytics_service.language_breakdown(
        session, context.business_id, date_from=start, date_to=end, agent_id=agent_id
    )
    intents = await analytics_service.intent_breakdown(
        session, context.business_id, date_from=start, date_to=end, agent_id=agent_id
    )

    points = [TimeseriesPoint.model_validate(p) for p in series]
    return CallAnalyticsOut(
        date_from=start,
        date_to=end,
        agent_id=agent_id,
        series=points,
        totals=_sum_series(points),
        languages=languages,
        intents=[IntentCount.model_validate(i) for i in intents],
    )


@router.get("/agents", response_model=list[AgentLeaderboardEntry])
async def agent_leaderboard(
    context: CurrentContext,
    session: DbSession,
    days: Annotated[int, Query(ge=1, le=365)] = 30,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
) -> list[AgentLeaderboardEntry]:
    rows = await analytics_service.agent_leaderboard(
        session, context.business_id, days=days, limit=limit
    )
    return [AgentLeaderboardEntry.model_validate(r) for r in rows]


@router.get("/languages", response_model=dict[str, int])
async def languages(
    context: CurrentContext,
    session: DbSession,
    days: Annotated[int, Query(ge=1, le=365)] = 30,
) -> dict[str, int]:
    return await analytics_service.language_breakdown(session, context.business_id, days=days)


@router.get("/intents", response_model=list[IntentCount])
async def intents(
    context: CurrentContext,
    session: DbSession,
    days: Annotated[int, Query(ge=1, le=365)] = 30,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> list[IntentCount]:
    rows = await analytics_service.intent_breakdown(
        session, context.business_id, days=days, limit=limit
    )
    return [IntentCount.model_validate(r) for r in rows]


@router.post("/rollup", response_model=RollupResult)
async def trigger_rollup(
    context: RequireAdmin,
    session: DbSession,
    day: Annotated[date | None, Query()] = None,
) -> RollupResult:
    """Recompute the daily rollup on demand — the nightly worker does this too."""
    target = day or (_today() - timedelta(days=1))
    rows = await analytics_service.rollup_all_agents(session, context.business_id, target)
    return RollupResult(
        business_id=context.business_id,
        date=target,
        # The first row is the business-wide roll-up, the rest are per agent.
        agents_rolled_up=max(0, len(rows) - 1),
    )


def _sum_series(points: list[TimeseriesPoint]) -> WindowMetrics:
    """Totals over the returned series, so the client need not re-add them."""
    total_calls = sum(p.total_calls for p in points)
    answered = sum(p.answered_calls for p in points)
    duration = sum(int(p.avg_duration_sec * p.total_calls) for p in points)
    # resolution_rate is a per-day ratio; weight it by that day's call volume.
    resolved = sum(int(round(p.resolution_rate * p.total_calls)) for p in points)
    return WindowMetrics(
        total_calls=total_calls,
        total_duration_sec=duration,
        avg_duration_sec=round(duration / total_calls, 2) if total_calls else 0.0,
        total_billable_minutes=sum(p.billable_minutes for p in points),
        total_cost_paise=sum(p.cost_paise for p in points),
        resolved_calls=resolved,
        resolution_rate=round(resolved / total_calls, 4) if total_calls else 0.0,
        inbound_calls=sum(p.inbound_calls for p in points),
        outbound_calls=sum(p.outbound_calls for p in points),
        answered_calls=answered,
        answer_rate=round(answered / total_calls, 4) if total_calls else 0.0,
        positive_sentiment=sum(p.positive_sentiment for p in points),
        negative_sentiment=sum(p.negative_sentiment for p in points),
    )
