"""Analytics response schemas (design doc §4.4)."""

from __future__ import annotations

import uuid
from datetime import date

from pydantic import BaseModel, Field


class WindowMetrics(BaseModel):
    """Aggregate metrics over one time window."""

    total_calls: int = 0
    total_duration_sec: int = 0
    avg_duration_sec: float = 0.0
    total_billable_minutes: int = 0
    total_cost_paise: int = 0
    resolved_calls: int = 0
    resolution_rate: float = 0.0
    inbound_calls: int = 0
    outbound_calls: int = 0
    answered_calls: int = 0
    answer_rate: float = 0.0
    positive_sentiment: int = 0
    negative_sentiment: int = 0
    handed_off_calls: int = 0


class DashboardDeltas(BaseModel):
    """Change against the immediately preceding window of the same length."""

    total_calls: float = 0.0
    resolution_rate: float = 0.0
    avg_duration_sec: float = 0.0


class DashboardSummary(WindowMetrics):
    period_days: int
    #: ISO timestamps bounding the window.
    from_: str = Field(alias="from")
    to: str
    active_agents: int = 0
    deltas: DashboardDeltas = Field(default_factory=DashboardDeltas)

    model_config = {"populate_by_name": True}


class TimeseriesPoint(BaseModel):
    date: str
    total_calls: int = 0
    inbound_calls: int = 0
    outbound_calls: int = 0
    answered_calls: int = 0
    avg_duration_sec: float = 0.0
    resolution_rate: float = 0.0
    billable_minutes: int = 0
    cost_paise: int = 0
    positive_sentiment: int = 0
    negative_sentiment: int = 0


class AgentLeaderboardEntry(BaseModel):
    agent_id: uuid.UUID | None = None
    agent_name: str
    total_calls: int = 0
    avg_duration_sec: float = 0.0
    resolved_calls: int = 0
    resolution_rate: float = 0.0
    avg_sentiment_score: float = 0.0
    billable_minutes: int = 0


class IntentCount(BaseModel):
    intent: str
    count: int


class CallAnalyticsOut(BaseModel):
    """The ``/analytics/calls`` payload: series plus its breakdowns."""

    date_from: date
    date_to: date
    agent_id: uuid.UUID | None = None
    series: list[TimeseriesPoint] = Field(default_factory=list)
    totals: WindowMetrics = Field(default_factory=WindowMetrics)
    languages: dict[str, int] = Field(default_factory=dict)
    intents: list[IntentCount] = Field(default_factory=list)


class RollupResult(BaseModel):
    business_id: uuid.UUID
    date: date
    agents_rolled_up: int = 0
