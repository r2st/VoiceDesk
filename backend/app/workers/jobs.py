"""Scheduled background jobs.

Every job is a plain async function over a session, so it can be called
directly from a test or a management command without a scheduler in the
picture. All of them are idempotent: re-running a job for the same period
recomputes rather than accumulates, which is what makes catch-up after an
outage safe.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any, cast
from zoneinfo import ZoneInfo

from sqlalchemy import delete, or_, select
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import get_logger
from app.models.business import Business, RefreshToken
from app.models.enums import AppointmentStatus, BusinessStatus, CrmPushStatus
from app.services import analytics_service, billing_service, recording_service

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class JobResult:
    """What a job did, for logging and for assertions in tests."""

    name: str
    detail: dict[str, Any] = field(default_factory=dict)


def today_ist() -> date:
    return datetime.now(ZoneInfo(settings.trai_timezone)).date()


def previous_month(month: str | None = None) -> str:
    """The ``YYYY-MM`` period before ``month`` (default: the current one)."""
    year, mon = (int(part) for part in (month or billing_service.current_month()).split("-"))
    return f"{year - 1}-12" if mon == 1 else f"{year}-{mon - 1:02d}"


async def _tenant_ids(session: AsyncSession) -> list[uuid.UUID]:
    """Every live tenant. Cancelled businesses are excluded — their data is
    frozen, so recomputing rollups and usage for them is pure waste."""
    rows = await session.execute(
        select(Business.id).where(
            Business.deleted_at.is_(None),
            Business.status != BusinessStatus.CANCELLED,
        )
    )
    return list(rows.scalars().all())


# --------------------------------------------------------------------------- #
# Analytics
# --------------------------------------------------------------------------- #
async def rollup_analytics(
    session: AsyncSession, *, day: date | None = None, lookback_days: int = 2
) -> JobResult:
    """Rebuild the daily analytics rows for a trailing window.

    The window overlaps deliberately. A call that ends near midnight has its
    duration and resolution stamped by a webhook that can arrive minutes or
    hours later, so yesterday's row is not final when yesterday ends. Rolling
    up the last two days on every run absorbs those late updates, and because
    ``rollup_day`` recomputes from ``call_logs`` the overlap costs nothing but
    a little query time.
    """
    if day is not None:
        days = [day]
    else:
        days = [today_ist() - timedelta(days=n) for n in range(lookback_days)]

    tenants = await _tenant_ids(session)
    rows = 0
    for business_id in tenants:
        for target in days:
            rows += len(await analytics_service.rollup_all_agents(session, business_id, target))

    detail = {
        "businesses": len(tenants),
        "days": [d.isoformat() for d in days],
        "rows_written": rows,
    }
    logger.info("Analytics rollup wrote %s row(s) for %s tenant(s)", rows, len(tenants))
    return JobResult("rollup_analytics", detail)


# --------------------------------------------------------------------------- #
# Recordings
# --------------------------------------------------------------------------- #
async def purge_expired_recordings(session: AsyncSession) -> JobResult:
    """Delete call audio past its retention date (DPDP data minimisation)."""
    purged = await recording_service.purge_expired(session)
    return JobResult("purge_expired_recordings", {"purged": purged})


# --------------------------------------------------------------------------- #
# Billing
# --------------------------------------------------------------------------- #
async def refresh_billing_usage(session: AsyncSession, *, month: str | None = None) -> JobResult:
    """Recompute the open billing cycle for every tenant.

    Keeps the in-dashboard usage figure close to real time without making the
    call path pay for a recalculation on every hangup.
    """
    period = month or billing_service.current_month()
    tenants = await _tenant_ids(session)
    minutes = 0
    for business_id in tenants:
        usage = await billing_service.recalculate_usage(session, business_id, period)
        minutes += usage.minutes_used
    return JobResult(
        "refresh_billing_usage",
        {"month": period, "businesses": len(tenants), "minutes_used": minutes},
    )


async def finalize_billing_month(session: AsyncSession, *, month: str | None = None) -> JobResult:
    """Close the previous billing cycle and assign invoice numbers.

    Runs daily rather than only on the 1st: if the worker was down at the turn
    of the month the next run still closes the period, and ``finalize_month``
    is a no-op once a cycle is already finalized.
    """
    period = month or previous_month()
    tenants = await _tenant_ids(session)
    finalized = 0
    total_paise = 0
    for business_id in tenants:
        usage = await billing_service.finalize_month(session, business_id, period)
        total_paise += usage.total_paise
        finalized += 1
    return JobResult(
        "finalize_billing_month",
        {"month": period, "finalized": finalized, "total_paise": total_paise},
    )


# --------------------------------------------------------------------------- #
# Tenant lifecycle
# --------------------------------------------------------------------------- #
async def expire_trials(session: AsyncSession, *, now: datetime | None = None) -> JobResult:
    """Suspend tenants whose trial window has closed.

    Only ``trial`` rows are touched — a business that converted to a paid plan
    has had its status moved off ``trial`` already, so a stale ``trial_ends_at``
    cannot suspend a paying customer.
    """
    moment = now or datetime.now(UTC)
    rows = (
        (
            await session.execute(
                select(Business).where(
                    Business.deleted_at.is_(None),
                    Business.status == BusinessStatus.TRIAL,
                    Business.trial_ends_at.is_not(None),
                    Business.trial_ends_at <= moment,
                )
            )
        )
        .scalars()
        .all()
    )

    for business in rows:
        business.status = BusinessStatus.SUSPENDED
        logger.info("Trial expired for business %s (%s)", business.slug, business.id)
    if rows:
        await session.flush()
    return JobResult("expire_trials", {"suspended": len(rows)})


# --------------------------------------------------------------------------- #
# Hygiene
# --------------------------------------------------------------------------- #
async def prune_refresh_tokens(session: AsyncSession, *, grace_days: int = 7) -> JobResult:
    """Hard-delete spent refresh tokens.

    These are credentials, not business records: once a token is expired or
    revoked it has no audit value, and keeping the hashes around only widens
    what a database compromise yields. The grace period leaves a short window
    in which token-reuse detection can still fire on a replayed token.
    """
    cutoff = datetime.now(UTC) - timedelta(days=grace_days)
    result = cast(
        CursorResult,
        await session.execute(
            delete(RefreshToken).where(
                or_(
                    RefreshToken.expires_at < cutoff,
                    RefreshToken.revoked_at.is_not(None) & (RefreshToken.revoked_at < cutoff),
                )
            )
        ),
    )
    deleted = int(result.rowcount or 0)
    if deleted:
        await session.flush()
        logger.info("Pruned %s spent refresh token(s)", deleted)
    return JobResult("prune_refresh_tokens", {"deleted": deleted})


# --------------------------------------------------------------------------- #
# Appointments
# --------------------------------------------------------------------------- #
async def send_appointment_reminders(
    session: AsyncSession, *, now: datetime | None = None
) -> JobResult:
    """WhatsApp a reminder for each booking starting in the next day.

    ``reminder_sent_at`` is stamped whether or not the provider accepted the
    message: a reminder is a courtesy, and retrying a failed send on every tick
    for the next 24 hours would spend far more than it recovers. A send that
    raises leaves the stamp unset so the next run tries once more.
    """
    from app.services import appointment_service
    from app.services.whatsapp import WhatsAppMessage, get_whatsapp_provider

    due = await appointment_service.due_for_reminder(session, now=now)
    if not due:
        return JobResult("send_appointment_reminders", {"sent": 0, "failed": 0})

    provider = get_whatsapp_provider()
    businesses: dict[uuid.UUID, Business] = {}
    sent = failed = 0

    for appointment in due:
        business = businesses.get(appointment.business_id)
        if business is None:
            business = await session.get(Business, appointment.business_id)
            if business is None or business.deleted_at is not None:
                continue
            businesses[appointment.business_id] = business

        config = appointment_service.scheduling.load_schedule_config(business.settings_json)
        try:
            result = await provider.send(
                WhatsAppMessage(
                    to_number=appointment.customer_phone,
                    body=appointment_service.reminder_text(appointment, business, config.timezone),
                    context={"appointment_id": str(appointment.id), "kind": "reminder"},
                )
            )
        except Exception as exc:
            logger.warning("Reminder for appointment %s failed: %s", appointment.id, exc)
            failed += 1
            continue

        appointment.reminder_sent_at = datetime.now(UTC)
        sent += result.accepted
        failed += not result.accepted

    await session.flush()
    logger.info("Appointment reminders: %s sent, %s failed", sent, failed)
    return JobResult("send_appointment_reminders", {"sent": sent, "failed": failed})


async def close_missed_appointments(
    session: AsyncSession, *, now: datetime | None = None
) -> JobResult:
    """Mark bookings nobody closed as no-shows once their slot is well past.

    Staff who mark the outcome themselves always win — only appointments still
    sitting in ``scheduled``/``confirmed`` hours after the slot ended are
    touched, so the calendar reflects reality instead of accumulating rows that
    look perpetually upcoming.
    """
    from app.services import appointment_service

    overdue = await appointment_service.overdue_without_outcome(session, now=now)
    for appointment in overdue:
        appointment.status = AppointmentStatus.NO_SHOW
    if overdue:
        await session.flush()
        logger.info("Closed %s missed appointment(s) as no-shows", len(overdue))
    return JobResult("close_missed_appointments", {"no_shows": len(overdue)})


# --------------------------------------------------------------------------- #
# Leads
# --------------------------------------------------------------------------- #
async def push_qualified_leads(session: AsyncSession, *, limit: int = 200) -> JobResult:
    """Deliver qualified leads to each tenant's CRM (design doc §4.6).

    A push is attempted per lead rather than per tenant so one unreachable CRM
    delays only its own tenant's leads. Every outcome — delivered, refused,
    unreachable — is written back to the row, so the pipeline view can show a
    salesperson that a lead has not reached their CRM instead of leaving them
    to discover it when the follow-up never happens.
    """
    from app.services import crm, lead_service

    due = await lead_service.due_for_crm_push(
        session, limit=limit, max_attempts=settings.crm_max_attempts
    )
    if not due:
        return JobResult("push_qualified_leads", {"sent": 0, "failed": 0, "skipped": 0})

    client = crm.get_crm_client()
    configs: dict[uuid.UUID, crm.CrmConfig | None] = {}
    sent = failed = skipped = 0

    for lead in due:
        if lead.business_id not in configs:
            business = await session.get(Business, lead.business_id)
            configs[lead.business_id] = (
                crm.load_crm_config(business.settings_json)
                if business is not None and business.deleted_at is None
                else None
            )
        config = configs[lead.business_id]

        # No destination is a settled state, not a failure: parking the lead as
        # NOT_CONFIGURED keeps it out of every later pass until the tenant
        # actually sets a webhook up.
        if config is None or not config.configured:
            lead.crm_status = CrmPushStatus.NOT_CONFIGURED
            skipped += 1
            continue

        if config.min_score is not None and lead.score < config.min_score:
            lead.crm_status = CrmPushStatus.NOT_CONFIGURED
            lead.crm_error = f"Below the tenant's CRM floor of {config.min_score}."
            skipped += 1
            continue

        lead.crm_attempts += 1
        try:
            result = await client.push(config, lead)
        except Exception as exc:  # a broken client must not strand the batch
            logger.warning("CRM push for lead %s raised: %s", lead.id, exc)
            result = crm.CrmResult(delivered=False, error=f"{type(exc).__name__}: {exc}"[:500])

        if result.delivered:
            lead.crm_status = CrmPushStatus.SENT
            lead.crm_pushed_at = datetime.now(UTC)
            lead.crm_reference = result.reference
            lead.crm_error = None
            sent += 1
        else:
            lead.crm_status = CrmPushStatus.FAILED
            lead.crm_error = (result.error or "CRM push failed.")[:500]
            failed += 1

    await session.flush()
    logger.info("CRM push: %s sent, %s failed, %s skipped", sent, failed, skipped)
    return JobResult("push_qualified_leads", {"sent": sent, "failed": failed, "skipped": skipped})
