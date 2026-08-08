"""Per-minute metering and monthly billing. All amounts are integer paise."""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import NotFoundError
from app.core.logging import get_logger
from app.core.tenancy import tenant_select
from app.models.analytics import BillingUsage
from app.models.business import Business
from app.models.call import CallLog, PhoneNumber
from app.models.enums import CallStatus, PhoneNumberStatus, PlanTier
from app.services.plans import Plan, apply_gst, get_plan

logger = get_logger(__name__)


def current_month(when: datetime | None = None) -> str:
    """Billing period key ``YYYY-MM`` in the business timezone (IST)."""
    moment = (when or datetime.now(UTC)).astimezone(ZoneInfo(settings.trai_timezone))
    return moment.strftime("%Y-%m")


def billable_minutes(duration_sec: int) -> int:
    """Calls are metered per started minute, rounded up. A 1-second call bills 1 minute."""
    if duration_sec <= 0:
        return 0
    return math.ceil(duration_sec / 60)


@dataclass(frozen=True, slots=True)
class MeterResult:
    minutes: int
    cost_paise: int
    plan: PlanTier


async def meter_call(session: AsyncSession, call: CallLog) -> MeterResult:
    """Compute and stamp the billable minutes and cost for a finished call.

    Only completed calls are billable — a busy signal or no-answer costs the
    business nothing. Re-metering the same call is a no-op, so webhook replays
    cannot double-charge.
    """
    business = await session.get(Business, call.business_id)
    plan = get_plan(business.plan if business else PlanTier.STARTER)

    if call.status not in {s.value for s in CallStatus.billable()}:
        call.billable_minutes = 0
        call.cost_paise = 0
        return MeterResult(0, 0, plan.tier)

    minutes = billable_minutes(call.duration_sec)
    cost = minutes * plan.per_minute_paise

    if call.billable_minutes == minutes and call.cost_paise == cost:
        return MeterResult(minutes, cost, plan.tier)

    call.billable_minutes = minutes
    call.cost_paise = cost
    await session.flush()
    return MeterResult(minutes, cost, plan.tier)


async def get_or_create_usage(
    session: AsyncSession, business_id: uuid.UUID, month: str | None = None
) -> BillingUsage:
    period = month or current_month()
    usage = (
        await session.execute(
            tenant_select(BillingUsage, business_id).where(BillingUsage.month == period)
        )
    ).scalar_one_or_none()
    if usage is not None:
        return usage

    business = await session.get(Business, business_id)
    if business is None or business.deleted_at is not None:
        raise NotFoundError("Business not found.")
    plan = get_plan(business.plan)

    usage = BillingUsage(
        business_id=business_id,
        month=period,
        plan_id=plan.tier,
        included_minutes=plan.included_minutes,
        base_fee_paise=plan.monthly_fee_paise,
    )
    session.add(usage)
    await session.flush()
    return usage


async def recalculate_usage(
    session: AsyncSession, business_id: uuid.UUID, month: str | None = None
) -> BillingUsage:
    """Recompute a billing cycle from the underlying call rows.

    Deriving totals from ``call_logs`` rather than incrementing a counter means
    the cycle is always consistent, even if a webhook was replayed or a call was
    re-metered after a correction.
    """
    period = month or current_month()
    usage = await get_or_create_usage(session, business_id, period)
    if usage.is_finalized:
        return usage

    plan = get_plan(usage.plan_id)
    start, end = month_bounds(period)

    row = (
        await session.execute(
            select(
                func.coalesce(func.sum(CallLog.billable_minutes), 0),
                func.count(CallLog.id),
            ).where(
                CallLog.business_id == business_id,
                CallLog.deleted_at.is_(None),
                CallLog.status == CallStatus.COMPLETED,
                CallLog.created_at >= start,
                CallLog.created_at < end,
            )
        )
    ).one()
    minutes_used, calls_count = int(row[0] or 0), int(row[1] or 0)

    rent = await session.scalar(
        select(func.coalesce(func.sum(PhoneNumber.monthly_rent_paise), 0)).where(
            PhoneNumber.business_id == business_id,
            PhoneNumber.deleted_at.is_(None),
            PhoneNumber.status == PhoneNumberStatus.ACTIVE,
        )
    )

    overage_minutes = max(0, minutes_used - plan.included_minutes)
    overage_paise = overage_minutes * plan.per_minute_paise
    amount = plan.monthly_fee_paise + overage_paise + int(rent or 0)
    tax = apply_gst(amount)

    usage.minutes_used = minutes_used
    usage.calls_count = calls_count
    usage.included_minutes = plan.included_minutes
    usage.overage_minutes = overage_minutes
    usage.base_fee_paise = plan.monthly_fee_paise
    usage.overage_paise = overage_paise
    usage.number_rent_paise = int(rent or 0)
    usage.amount_paise = amount
    usage.tax_paise = tax
    usage.total_paise = amount + tax
    await session.flush()
    return usage


def month_bounds(month: str) -> tuple[datetime, datetime]:
    """UTC half-open ``[start, end)`` bounds for a ``YYYY-MM`` IST period."""
    tz = ZoneInfo(settings.trai_timezone)
    year, mon = (int(part) for part in month.split("-"))
    start_local = datetime(year, mon, 1, tzinfo=tz)
    end_local = (
        datetime(year + 1, 1, 1, tzinfo=tz)
        if mon == 12
        else datetime(year, mon + 1, 1, tzinfo=tz)
    )
    return start_local.astimezone(UTC), end_local.astimezone(UTC)


async def finalize_month(
    session: AsyncSession, business_id: uuid.UUID, month: str
) -> BillingUsage:
    """Close a billing cycle and assign an invoice number. Idempotent."""
    usage = await recalculate_usage(session, business_id, month)
    if usage.is_finalized:
        return usage
    usage.is_finalized = True
    usage.finalized_at = datetime.now(UTC)
    usage.invoice_number = f"VD-{month.replace('-', '')}-{str(business_id)[:8].upper()}"
    await session.flush()
    logger.info("Finalized billing for %s %s: %s paise", business_id, month, usage.total_paise)
    return usage


async def list_usage(
    session: AsyncSession, business_id: uuid.UUID, *, limit: int = 12
) -> list[BillingUsage]:
    result = await session.execute(
        tenant_select(BillingUsage, business_id)
        .order_by(BillingUsage.month.desc())
        .limit(limit)
    )
    return list(result.scalars().all())


async def quota_status(
    session: AsyncSession, business_id: uuid.UUID, month: str | None = None
) -> dict:
    """Where the tenant stands against the included minutes in its plan."""
    usage = await recalculate_usage(session, business_id, month)
    plan = get_plan(usage.plan_id)
    remaining = max(0, plan.included_minutes - usage.minutes_used)
    pct = (usage.minutes_used / plan.included_minutes * 100) if plan.included_minutes else 0.0
    return {
        "month": usage.month,
        "plan": plan.tier.value,
        "minutes_used": usage.minutes_used,
        "included_minutes": plan.included_minutes,
        "remaining_minutes": remaining,
        "overage_minutes": usage.overage_minutes,
        "utilization_pct": round(pct, 2),
        "in_overage": usage.overage_minutes > 0,
    }


def projected_month_end_paise(usage: BillingUsage, today: date | None = None) -> int:
    """Straight-line projection of the cycle total from usage so far."""
    reference = today or datetime.now(ZoneInfo(settings.trai_timezone)).date()
    start, end = month_bounds(usage.month)
    days_in_month = (end - start).days
    day_of_month = min(max(reference.day, 1), days_in_month)
    if day_of_month >= days_in_month:
        return usage.total_paise

    plan = get_plan(usage.plan_id)
    projected_minutes = round(usage.minutes_used * days_in_month / day_of_month)
    overage = max(0, projected_minutes - plan.included_minutes)
    amount = plan.monthly_fee_paise + overage * plan.per_minute_paise + usage.number_rent_paise
    return amount + apply_gst(amount)
