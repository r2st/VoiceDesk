"""Aggregated analytics rollups and per-minute billing records."""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, SoftDeleteMixin, TimestampMixin, UUIDPrimaryKeyMixin
from app.db.types import GUID, JSONBType
from app.models.enums import PlanTier


class DailyAnalytics(Base, UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin):
    """One row per business per agent per day, produced by the rollup job."""

    __tablename__ = "analytics"

    business_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: NULL is the business-wide roll-up across all agents.
    agent_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("voice_agents.id", ondelete="CASCADE"), nullable=True, index=True
    )
    date: Mapped[date] = mapped_column(Date, nullable=False, index=True)

    total_calls: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    inbound_calls: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    outbound_calls: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    answered_calls: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failed_calls: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    total_duration_sec: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    avg_duration_sec: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    total_billable_minutes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_cost_paise: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)

    resolved_calls: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    escalated_calls: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    handed_off_calls: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    resolution_rate: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)

    positive_sentiment: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    neutral_sentiment: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    negative_sentiment: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    avg_confidence: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)

    #: {"hi": 42, "en": 18, ...}
    language_breakdown: Mapped[dict] = mapped_column(JSONBType, default=dict, nullable=False)
    #: {"book_appointment": 12, ...}
    intent_breakdown: Mapped[dict] = mapped_column(JSONBType, default=dict, nullable=False)

    __table_args__ = (
        UniqueConstraint("business_id", "agent_id", "date", name="uq_analytics_scope_date"),
        Index("ix_analytics_business_date", "business_id", "date"),
    )


class BillingUsage(Base, UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin):
    """Monthly billing cycle for a business. All money is in integer paise."""

    __tablename__ = "billing_usage"

    business_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: Billing period as ``YYYY-MM`` in Asia/Kolkata.
    month: Mapped[str] = mapped_column(String(7), nullable=False, index=True)
    plan_id: Mapped[str] = mapped_column(String(30), default=PlanTier.STARTER, nullable=False)

    minutes_used: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    included_minutes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    overage_minutes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    calls_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    base_fee_paise: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    overage_paise: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    number_rent_paise: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    #: base + overage + rent, before tax.
    amount_paise: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    tax_paise: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    total_paise: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="INR", nullable=False)

    is_finalized: Mapped[bool] = mapped_column(default=False, nullable=False)
    finalized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    invoice_number: Mapped[str | None] = mapped_column(String(40), nullable=True)

    __table_args__ = (
        UniqueConstraint("business_id", "month", name="uq_billing_usage_business_id_month"),
    )
