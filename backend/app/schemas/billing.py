"""Billing and plan schemas. All money is integer paise (design doc §6.1)."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.models.enums import PlanTier
from app.schemas.common import ORMModel


class PlanOut(BaseModel):
    tier: PlanTier
    name: str
    monthly_fee_paise: int
    per_minute_paise: int
    included_minutes: int
    max_agents: int | None = None
    max_languages: int | None = None
    features: list[str] = Field(default_factory=list)
    annual_fee_paise: int = 0
    is_custom: bool = False


class UsageOut(ORMModel):
    id: uuid.UUID
    business_id: uuid.UUID
    month: str
    plan_id: PlanTier
    minutes_used: int
    included_minutes: int
    overage_minutes: int
    calls_count: int
    base_fee_paise: int
    overage_paise: int
    number_rent_paise: int
    amount_paise: int
    tax_paise: int
    total_paise: int
    currency: str
    is_finalized: bool
    finalized_at: datetime | None
    invoice_number: str | None
    created_at: datetime


class CurrentUsageOut(BaseModel):
    """``/billing/usage`` — the live cycle plus a straight-line projection."""

    usage: UsageOut
    plan: PlanOut
    quota: QuotaStatus
    projected_total_paise: int = 0


class QuotaStatus(BaseModel):
    month: str
    plan: PlanTier
    minutes_used: int
    included_minutes: int
    remaining_minutes: int
    overage_minutes: int
    utilization_pct: float
    in_overage: bool


CurrentUsageOut.model_rebuild()
