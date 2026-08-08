"""``/api/v1/billing`` — usage, quota and plan catalogue (design doc §6.1)."""

from __future__ import annotations

import re
from typing import Annotated

from fastapi import APIRouter, Query

from app.core.deps import CurrentContext, DbSession, RequireAdmin
from app.core.errors import ValidationError
from app.models.enums import PlanTier
from app.schemas.billing import CurrentUsageOut, PlanOut, QuotaStatus, UsageOut
from app.services import billing_service
from app.services.plans import PLANS, get_plan

router = APIRouter(prefix="/billing", tags=["billing"])

MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def _validated_month(month: str | None) -> str | None:
    if month is None:
        return None
    if not MONTH_RE.match(month):
        raise ValidationError("'month' must be formatted as YYYY-MM.")
    return month


def _plan_out(tier: PlanTier | str) -> PlanOut:
    plan = get_plan(tier)
    return PlanOut(
        tier=plan.tier,
        name=plan.name,
        monthly_fee_paise=plan.monthly_fee_paise,
        per_minute_paise=plan.per_minute_paise,
        included_minutes=plan.included_minutes,
        max_agents=plan.max_agents,
        max_languages=plan.max_languages,
        features=list(plan.features),
        annual_fee_paise=plan.annual_fee_paise(),
        is_custom=plan.is_custom,
    )


@router.get("/usage", response_model=CurrentUsageOut)
async def current_usage(
    context: CurrentContext,
    session: DbSession,
    month: Annotated[str | None, Query(description="YYYY-MM; defaults to the open cycle")] = None,
) -> CurrentUsageOut:
    """The live billing cycle, recomputed from call records on read."""
    target = _validated_month(month)
    usage = await billing_service.recalculate_usage(session, context.business_id, target)
    quota = await billing_service.quota_status(session, context.business_id, target)
    return CurrentUsageOut(
        usage=UsageOut.model_validate(usage),
        plan=_plan_out(usage.plan_id),
        quota=QuotaStatus.model_validate(quota),
        projected_total_paise=billing_service.projected_month_end_paise(usage),
    )


@router.get("/quota", response_model=QuotaStatus)
async def quota(
    context: CurrentContext,
    session: DbSession,
    month: Annotated[str | None, Query()] = None,
) -> QuotaStatus:
    """Where the tenant stands against its included minutes."""
    status = await billing_service.quota_status(
        session, context.business_id, _validated_month(month)
    )
    return QuotaStatus.model_validate(status)


@router.get("/history", response_model=list[UsageOut])
async def usage_history(
    context: CurrentContext,
    session: DbSession,
    limit: Annotated[int, Query(ge=1, le=36)] = 12,
) -> list[UsageOut]:
    rows = await billing_service.list_usage(session, context.business_id, limit=limit)
    return [UsageOut.model_validate(r) for r in rows]


@router.get("/plans", response_model=list[PlanOut])
async def list_plans(context: CurrentContext) -> list[PlanOut]:
    """The public plan catalogue, for the upgrade screen."""
    return [_plan_out(tier) for tier in PLANS]


@router.post("/finalize", response_model=UsageOut)
async def finalize(
    context: RequireAdmin,
    session: DbSession,
    month: Annotated[str, Query(description="YYYY-MM cycle to close")],
) -> UsageOut:
    """Close a billing cycle and assign it an invoice number.

    Idempotent: a cycle that is already finalized is returned unchanged.
    """
    validated = _validated_month(month)
    assert validated is not None  # a non-optional query param, validated above
    usage = await billing_service.finalize_month(session, context.business_id, validated)
    return UsageOut.model_validate(usage)
