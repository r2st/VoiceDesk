"""Lead qualification schemas (design doc §4.9)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, EmailStr, Field, field_validator

from app.models.enums import (
    BANTDimension,
    CrmPushStatus,
    Language,
    LeadSource,
    LeadStatus,
    LeadTier,
)
from app.schemas.common import ORMModel, normalize_phone


class BantAnswersIn(BaseModel):
    """What the caller said for each dimension. Any of them may be missing."""

    budget: Annotated[str | None, Field(default=None, max_length=2000)] = None
    authority: Annotated[str | None, Field(default=None, max_length=2000)] = None
    need: Annotated[str | None, Field(default=None, max_length=2000)] = None
    timeline: Annotated[str | None, Field(default=None, max_length=2000)] = None


class LeadCreate(BaseModel):
    contact_name: Annotated[str, Field(min_length=2, max_length=200)]
    contact_phone: Annotated[str, Field(min_length=6, max_length=20)]
    contact_email: EmailStr | None = None
    company: Annotated[str | None, Field(default=None, max_length=200)] = None
    interest: Annotated[str | None, Field(default=None, max_length=300)] = None
    answers: BantAnswersIn = Field(default_factory=BantAnswersIn)
    language: Language | None = None
    notes: Annotated[str | None, Field(default=None, max_length=4000)] = None
    source: LeadSource = LeadSource.DASHBOARD
    agent_id: uuid.UUID | None = None
    call_id: uuid.UUID | None = None

    @field_validator("contact_phone")
    @classmethod
    def _phone(cls, value: str) -> str:
        return normalize_phone(value)


class LeadUpdate(BaseModel):
    """Staff edits. Changing an answer re-scores the lead."""

    contact_name: Annotated[str | None, Field(default=None, min_length=2, max_length=200)] = None
    contact_email: EmailStr | None = None
    company: Annotated[str | None, Field(default=None, max_length=200)] = None
    interest: Annotated[str | None, Field(default=None, max_length=300)] = None
    answers: BantAnswersIn | None = None
    notes: Annotated[str | None, Field(default=None, max_length=4000)] = None
    status: LeadStatus | None = None


class DimensionScoreOut(BaseModel):
    dimension: BANTDimension
    answer: str | None
    score: float
    percent: int
    reason: str


class LeadOut(ORMModel):
    id: uuid.UUID
    business_id: uuid.UUID
    agent_id: uuid.UUID | None
    call_id: uuid.UUID | None
    contact_name: str
    contact_phone: str
    contact_email: str | None
    company: str | None
    interest: str | None
    status: LeadStatus
    tier: LeadTier
    source: LeadSource
    language: Language | None
    score: int
    budget_score: float
    authority_score: float
    need_score: float
    timeline_score: float
    budget_answer: str | None
    authority_answer: str | None
    need_answer: str | None
    timeline_answer: str | None
    rationale: dict
    notes: str | None
    qualified_at: datetime | None
    crm_status: CrmPushStatus
    crm_pushed_at: datetime | None
    crm_reference: str | None
    crm_error: str | None
    created_at: datetime


class LeadDetailOut(LeadOut):
    """A lead with its scoring broken out per dimension."""

    breakdown: list[DimensionScoreOut] = Field(default_factory=list)


class QualificationWeightsUpdate(BaseModel):
    """Per-tenant scoring rules, stored on ``Business.settings_json``."""

    budget: Annotated[int | None, Field(default=None, ge=0, le=100)] = None
    authority: Annotated[int | None, Field(default=None, ge=0, le=100)] = None
    need: Annotated[int | None, Field(default=None, ge=0, le=100)] = None
    timeline: Annotated[int | None, Field(default=None, ge=0, le=100)] = None
    hot_at: Annotated[int | None, Field(default=None, ge=1, le=100)] = None
    warm_at: Annotated[int | None, Field(default=None, ge=1, le=100)] = None
    qualify_at: Annotated[int | None, Field(default=None, ge=1, le=100)] = None
    currency_floor: Annotated[int | None, Field(default=None, ge=1)] = None


class QualificationConfigOut(BaseModel):
    weights: dict[str, int]
    hot_at: int
    warm_at: int
    qualify_at: int
    currency_floor: int


class PipelineSummaryOut(BaseModel):
    """Counts for the pipeline header on the dashboard."""

    total: int
    by_tier: dict[str, int]
    by_status: dict[str, int]
    average_score: float
    qualified_rate: float
