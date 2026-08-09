"""Appointment booking, availability and lifecycle. Every query is tenant-scoped.

Two invariants hold regardless of who is booking — the voice agent mid-call,
dashboard staff, or the public API:

* a slot never exceeds the tenant's configured capacity, and
* a booking outside published hours requires an explicit staff override.

Capacity is checked inside a Redis lock keyed on the tenant and the day, so two
concurrent calls asking for the same 10:00 slot cannot both win.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import Select, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import locks
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.logging import get_logger, mask_phone
from app.core.tenancy import get_owned_or_404, tenant_select
from app.core.timeutil import ensure_utc
from app.models.appointment import Appointment
from app.models.business import Business
from app.models.enums import AppointmentStatus
from app.models.voice_agent import VoiceAgent
from app.schemas.appointment import (
    AppointmentCreate,
    AppointmentReschedule,
    AppointmentUpdate,
    BusinessHoursUpdate,
)
from app.services import scheduling
from app.services.scheduling import ScheduleConfig

logger = get_logger(__name__)

#: How long a booking may hold the per-day lock. Comfortably longer than the
#: capacity query, short enough that a dead replica frees the day quickly.
BOOKING_LOCK_TTL_SECONDS = 10.0

#: Status transitions staff are allowed to make by hand. Cancellation has its
#: own endpoint because it carries a reason.
ALLOWED_TRANSITIONS: dict[AppointmentStatus, set[AppointmentStatus]] = {
    AppointmentStatus.SCHEDULED: {
        AppointmentStatus.CONFIRMED,
        AppointmentStatus.COMPLETED,
        AppointmentStatus.NO_SHOW,
        AppointmentStatus.CANCELLED,
    },
    AppointmentStatus.CONFIRMED: {
        AppointmentStatus.COMPLETED,
        AppointmentStatus.NO_SHOW,
        AppointmentStatus.CANCELLED,
    },
    AppointmentStatus.COMPLETED: set(),
    AppointmentStatus.CANCELLED: set(),
    AppointmentStatus.NO_SHOW: {AppointmentStatus.COMPLETED},
}


@dataclass(frozen=True, slots=True)
class Slot:
    start: datetime
    end: datetime
    remaining_capacity: int


async def get_config(session: AsyncSession, business_id: uuid.UUID) -> ScheduleConfig:
    """Load the tenant's calendar rules."""
    business = await _get_business(session, business_id)
    return scheduling.load_schedule_config(business.settings_json)


async def _get_business(session: AsyncSession, business_id: uuid.UUID) -> Business:
    business = await session.get(Business, business_id)
    if business is None or business.deleted_at is not None:
        raise NotFoundError("Business not found.")
    return business


# --------------------------------------------------------------------------- #
# Availability
# --------------------------------------------------------------------------- #
async def available_slots(
    session: AsyncSession,
    business_id: uuid.UUID,
    day: date,
    *,
    duration_minutes: int | None = None,
    config: ScheduleConfig | None = None,
    now: datetime | None = None,
) -> tuple[list[Slot], ScheduleConfig]:
    """Bookable slots on ``day``, excluding those already at capacity.

    Slots in the past, inside the minimum-notice window, or beyond the booking
    horizon are dropped — the caller is only ever offered times it can take.
    """
    config = config or await get_config(session, business_id)
    duration = duration_minutes or config.slot_minutes
    _validate_duration(duration)

    candidates = scheduling.candidate_slots(config, day, duration)
    if not candidates:
        return [], config

    earliest, latest_date = scheduling.booking_horizon(config, now)
    if day > latest_date:
        return [], config

    booked = await _booked_in_range(
        session,
        business_id,
        candidates[0],
        candidates[-1] + timedelta(minutes=duration),
    )

    slots: list[Slot] = []
    for start in candidates:
        if start < earliest:
            continue
        end = start + timedelta(minutes=duration)
        taken = sum(1 for b_start, b_end in booked if b_start < end and b_end > start)
        remaining = config.capacity_per_slot - taken
        if remaining > 0:
            slots.append(Slot(start=start, end=end, remaining_capacity=remaining))
    return slots, config


async def next_available_slots(
    session: AsyncSession,
    business_id: uuid.UUID,
    *,
    limit: int = 3,
    duration_minutes: int | None = None,
    search_days: int = 14,
    now: datetime | None = None,
) -> list[Slot]:
    """The soonest ``limit`` openings, scanning forward day by day.

    Used by the voice agent to offer concrete alternatives when the caller's
    preferred time is gone.
    """
    config = await get_config(session, business_id)
    start_day = (now or config.now()).astimezone(config.zone).date()
    horizon = min(search_days, config.max_advance_days)

    found: list[Slot] = []
    for offset in range(horizon + 1):
        day = start_day + timedelta(days=offset)
        if not config.is_open_on(day):
            continue
        slots, _ = await available_slots(
            session,
            business_id,
            day,
            duration_minutes=duration_minutes,
            config=config,
            now=now,
        )
        found.extend(slots)
        if len(found) >= limit:
            break
    return found[:limit]


async def _booked_in_range(
    session: AsyncSession,
    business_id: uuid.UUID,
    window_start: datetime,
    window_end: datetime,
    *,
    exclude_id: uuid.UUID | None = None,
) -> list[tuple[datetime, datetime]]:
    """Occupied intervals overlapping the window, as aware UTC pairs.

    The range predicate is deliberately widened by the longest bookable
    appointment so that a long booking starting before the window is still
    seen — the index is on ``scheduled_at``, not on the (uncomputed) end.
    """
    lower = ensure_utc(window_start) - timedelta(minutes=scheduling.MAX_DURATION_MINUTES)
    stmt = tenant_select(Appointment, business_id).where(
        Appointment.scheduled_at >= lower,
        Appointment.scheduled_at < ensure_utc(window_end),
        Appointment.status.in_([s.value for s in AppointmentStatus.active()]),
    )
    if exclude_id is not None:
        stmt = stmt.where(Appointment.id != exclude_id)

    rows = (await session.execute(stmt)).scalars().all()
    intervals: list[tuple[datetime, datetime]] = []
    for row in rows:
        start = ensure_utc(row.scheduled_at)
        intervals.append((start, start + timedelta(minutes=row.duration_minutes)))
    return intervals


async def _capacity_taken(
    session: AsyncSession,
    business_id: uuid.UUID,
    start: datetime,
    duration_minutes: int,
    *,
    exclude_id: uuid.UUID | None = None,
) -> int:
    end = ensure_utc(start) + timedelta(minutes=duration_minutes)
    booked = await _booked_in_range(session, business_id, start, end, exclude_id=exclude_id)
    return sum(1 for b_start, b_end in booked if b_start < end and b_end > ensure_utc(start))


# --------------------------------------------------------------------------- #
# Booking
# --------------------------------------------------------------------------- #
async def book(
    session: AsyncSession,
    business_id: uuid.UUID,
    payload: AppointmentCreate,
    *,
    now: datetime | None = None,
) -> Appointment:
    """Book a slot, or raise ``ConflictError`` if it is already full."""
    config = await get_config(session, business_id)
    _validate_duration(payload.duration_minutes)

    start = ensure_utc(payload.scheduled_at)
    _validate_timing(config, start, payload.duration_minutes, payload.override_hours, now)

    if payload.agent_id is not None:
        await get_owned_or_404(
            session, VoiceAgent, payload.agent_id, business_id, label="Voice agent"
        )

    local_day = start.astimezone(config.zone).date()
    async with locks.guard(
        _day_lock(business_id, local_day), ttl_seconds=BOOKING_LOCK_TTL_SECONDS
    ) as held:
        if not held:
            raise ConflictError(
                "Another booking for this day is in progress. Please retry.",
                details={"date": local_day.isoformat()},
            )
        taken = await _capacity_taken(session, business_id, start, payload.duration_minutes)
        if taken >= config.capacity_per_slot:
            raise ConflictError(
                "That slot is already fully booked.",
                details={
                    "scheduled_at": start.isoformat(),
                    "capacity_per_slot": config.capacity_per_slot,
                },
            )

        appointment = Appointment(
            business_id=business_id,
            agent_id=payload.agent_id,
            call_id=payload.call_id,
            customer_name=payload.customer_name.strip(),
            customer_phone=payload.customer_phone,
            customer_email=payload.customer_email,
            service=payload.service,
            scheduled_at=start,
            duration_minutes=payload.duration_minutes,
            status=AppointmentStatus.SCHEDULED,
            source=payload.source,
            language=payload.language.value if payload.language else None,
            notes=payload.notes,
            metadata_json={},
        )
        session.add(appointment)
        await session.flush()

    logger.info(
        "Booked appointment %s for %s at %s (source=%s)",
        appointment.id,
        mask_phone(appointment.customer_phone),
        start.isoformat(),
        payload.source,
    )
    return appointment


async def reschedule(
    session: AsyncSession,
    business_id: uuid.UUID,
    appointment_id: uuid.UUID,
    payload: AppointmentReschedule,
    *,
    now: datetime | None = None,
) -> Appointment:
    """Move an appointment, keeping the original time in its history."""
    appointment = await get_appointment(session, business_id, appointment_id)
    _require_active(appointment, "rescheduled")

    config = await get_config(session, business_id)
    duration = payload.duration_minutes or appointment.duration_minutes
    _validate_duration(duration)

    start = ensure_utc(payload.scheduled_at)
    _validate_timing(config, start, duration, payload.override_hours, now)

    previous = ensure_utc(appointment.scheduled_at)
    local_day = start.astimezone(config.zone).date()
    async with locks.guard(
        _day_lock(business_id, local_day), ttl_seconds=BOOKING_LOCK_TTL_SECONDS
    ) as held:
        if not held:
            raise ConflictError(
                "Another booking for this day is in progress. Please retry.",
                details={"date": local_day.isoformat()},
            )
        taken = await _capacity_taken(
            session, business_id, start, duration, exclude_id=appointment.id
        )
        if taken >= config.capacity_per_slot:
            raise ConflictError(
                "That slot is already fully booked.",
                details={"scheduled_at": start.isoformat()},
            )

        history = list((appointment.metadata_json or {}).get("reschedule_history") or [])
        history.append(
            {
                "from": previous.isoformat(),
                "to": start.isoformat(),
                "reason": payload.reason,
                "at": datetime.now(UTC).isoformat(),
            }
        )
        appointment.metadata_json = {
            **(appointment.metadata_json or {}),
            "reschedule_history": history,
        }
        appointment.scheduled_at = start
        appointment.duration_minutes = duration
        # A rescheduled booking needs re-confirming, and its old reminder is void.
        appointment.status = AppointmentStatus.SCHEDULED
        appointment.confirmed_at = None
        appointment.reminder_sent_at = None
        await session.flush()

    return appointment


async def cancel(
    session: AsyncSession,
    business_id: uuid.UUID,
    appointment_id: uuid.UUID,
    reason: str | None = None,
) -> Appointment:
    appointment = await get_appointment(session, business_id, appointment_id)
    if appointment.status == AppointmentStatus.CANCELLED:
        return appointment
    _require_active(appointment, "cancelled")

    appointment.status = AppointmentStatus.CANCELLED
    appointment.cancelled_at = datetime.now(UTC)
    appointment.cancellation_reason = reason
    await session.flush()
    return appointment


async def set_status(
    session: AsyncSession,
    business_id: uuid.UUID,
    appointment_id: uuid.UUID,
    status: AppointmentStatus,
) -> Appointment:
    appointment = await get_appointment(session, business_id, appointment_id)
    current = AppointmentStatus(appointment.status)
    if status == current:
        return appointment
    if status not in ALLOWED_TRANSITIONS[current]:
        raise ValidationError(
            f"An appointment cannot move from '{current}' to '{status}'.",
            details={"from": current.value, "to": status.value},
        )

    appointment.status = status
    if status is AppointmentStatus.CONFIRMED:
        appointment.confirmed_at = datetime.now(UTC)
    elif status is AppointmentStatus.CANCELLED:
        appointment.cancelled_at = datetime.now(UTC)
    await session.flush()
    return appointment


async def update_details(
    session: AsyncSession,
    business_id: uuid.UUID,
    appointment_id: uuid.UUID,
    payload: AppointmentUpdate,
) -> Appointment:
    """Edit customer details. Timing changes go through ``reschedule``."""
    appointment = await get_appointment(session, business_id, appointment_id)
    fields = payload.model_dump(exclude_unset=True)
    for field, value in fields.items():
        if field == "language":
            appointment.language = value.value if value is not None else None
        else:
            setattr(appointment, field, value)
    await session.flush()
    return appointment


async def soft_delete(
    session: AsyncSession, business_id: uuid.UUID, appointment_id: uuid.UUID
) -> Appointment:
    appointment = await get_appointment(session, business_id, appointment_id)
    appointment.deleted_at = datetime.now(UTC)
    await session.flush()
    return appointment


# --------------------------------------------------------------------------- #
# Reads
# --------------------------------------------------------------------------- #
async def get_appointment(
    session: AsyncSession, business_id: uuid.UUID, appointment_id: uuid.UUID
) -> Appointment:
    return await get_owned_or_404(
        session, Appointment, appointment_id, business_id, label="Appointment"
    )


async def list_appointments(
    session: AsyncSession,
    business_id: uuid.UUID,
    *,
    status: AppointmentStatus | None = None,
    agent_id: uuid.UUID | None = None,
    customer_phone: str | None = None,
    starts_after: datetime | None = None,
    starts_before: datetime | None = None,
    search: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[Appointment], int]:
    stmt = _filtered(
        tenant_select(Appointment, business_id),
        status=status,
        agent_id=agent_id,
        customer_phone=customer_phone,
        starts_after=starts_after,
        starts_before=starts_before,
        search=search,
    )
    count_stmt = _filtered(
        select(func.count())
        .select_from(Appointment)
        .where(Appointment.business_id == business_id, Appointment.deleted_at.is_(None)),
        status=status,
        agent_id=agent_id,
        customer_phone=customer_phone,
        starts_after=starts_after,
        starts_before=starts_before,
        search=search,
    )
    total = await session.scalar(count_stmt) or 0
    rows = (
        (
            await session.execute(
                stmt.order_by(Appointment.scheduled_at.asc()).limit(limit).offset(offset)
            )
        )
        .scalars()
        .all()
    )
    return list(rows), total


def _filtered(
    stmt: Select,
    *,
    status: AppointmentStatus | None,
    agent_id: uuid.UUID | None,
    customer_phone: str | None,
    starts_after: datetime | None,
    starts_before: datetime | None,
    search: str | None,
) -> Select:
    if status is not None:
        stmt = stmt.where(Appointment.status == status.value)
    if agent_id is not None:
        stmt = stmt.where(Appointment.agent_id == agent_id)
    if customer_phone:
        stmt = stmt.where(Appointment.customer_phone == customer_phone)
    if starts_after is not None:
        stmt = stmt.where(Appointment.scheduled_at >= ensure_utc(starts_after))
    if starts_before is not None:
        stmt = stmt.where(Appointment.scheduled_at < ensure_utc(starts_before))
    if search:
        pattern = f"%{search.strip()}%"
        stmt = stmt.where(
            or_(
                Appointment.customer_name.ilike(pattern),
                Appointment.customer_phone.ilike(pattern),
                Appointment.service.ilike(pattern),
            )
        )
    return stmt


async def upcoming_for_phone(
    session: AsyncSession,
    business_id: uuid.UUID,
    customer_phone: str,
    *,
    now: datetime | None = None,
    limit: int = 5,
) -> list[Appointment]:
    """Live bookings for a caller — what the agent greets them about."""
    moment = ensure_utc(now or datetime.now(UTC))
    stmt = (
        tenant_select(Appointment, business_id)
        .where(
            Appointment.customer_phone == customer_phone,
            Appointment.scheduled_at >= moment,
            Appointment.status.in_([s.value for s in AppointmentStatus.active()]),
        )
        .order_by(Appointment.scheduled_at.asc())
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all())


# --------------------------------------------------------------------------- #
# Tenant calendar settings
# --------------------------------------------------------------------------- #
async def update_schedule_config(
    session: AsyncSession, business_id: uuid.UUID, payload: BusinessHoursUpdate
) -> ScheduleConfig:
    """Persist calendar settings onto ``Business.settings_json``."""
    business = await _get_business(session, business_id)
    current = dict(business.settings_json or {})

    if payload.hours:
        current["business_hours"] = {
            key: [
                [start.isoformat(timespec="minutes"), end.isoformat(timespec="minutes")]
                for start, end in windows
            ]
            for key, windows in payload.hours.items()
        }

    options = dict(current.get("appointments") or {})
    for field in ("slot_minutes", "capacity_per_slot", "min_notice_minutes", "max_advance_days"):
        value = getattr(payload, field)
        if value is not None:
            options[field] = value
    if payload.timezone is not None:
        options["timezone"] = payload.timezone
    if payload.closed_dates is not None:
        options["closed_dates"] = [day.isoformat() for day in payload.closed_dates]
    if options:
        current["appointments"] = options

    business.settings_json = current
    await session.flush()
    return scheduling.load_schedule_config(current)


# --------------------------------------------------------------------------- #
# Validation helpers
# --------------------------------------------------------------------------- #
def _validate_duration(duration_minutes: int) -> None:
    if duration_minutes <= 0 or duration_minutes > scheduling.MAX_DURATION_MINUTES:
        raise ValidationError(
            f"Duration must be between 1 and {scheduling.MAX_DURATION_MINUTES} minutes."
        )


def _validate_timing(
    config: ScheduleConfig,
    start: datetime,
    duration_minutes: int,
    override_hours: bool,
    now: datetime | None,
) -> None:
    earliest, latest_date = scheduling.booking_horizon(config, now)
    local = start.astimezone(config.zone)

    if start < ensure_utc(earliest):
        raise ValidationError(
            "That time is too soon to book.",
            details={
                "earliest": earliest.isoformat(),
                "min_notice_minutes": config.min_notice_minutes,
            },
        )
    if local.date() > latest_date:
        raise ValidationError(
            f"Bookings are accepted up to {config.max_advance_days} days ahead.",
            details={"latest_date": latest_date.isoformat()},
        )
    if not override_hours and not scheduling.within_business_hours(config, start, duration_minutes):
        raise ValidationError(
            "That time is outside the business's opening hours.",
            details={
                "scheduled_at": local.isoformat(),
                "windows": [
                    [w[0].isoformat(timespec="minutes"), w[1].isoformat(timespec="minutes")]
                    for w in config.windows_for(local.date())
                ],
            },
        )


def _require_active(appointment: Appointment, action: str) -> None:
    if AppointmentStatus(appointment.status) in AppointmentStatus.terminal():
        raise ValidationError(
            f"A {appointment.status} appointment cannot be {action}.",
            details={"status": appointment.status},
        )


def _day_lock(business_id: uuid.UUID, day: date) -> str:
    return f"appointments:{business_id}:{day.isoformat()}"


# --------------------------------------------------------------------------- #
# Scheduled maintenance
# --------------------------------------------------------------------------- #
#: Appointments are reminded once, this far ahead of the slot.
REMINDER_LEAD_HOURS = 24

#: How long after a slot ends before an unclosed booking counts as a no-show.
#: Wide enough that staff who close the appointment late are not overruled.
NO_SHOW_GRACE_HOURS = 2


async def due_for_reminder(
    session: AsyncSession,
    *,
    now: datetime | None = None,
    lead_hours: int = REMINDER_LEAD_HOURS,
    limit: int = 500,
) -> list[Appointment]:
    """Live bookings starting within the lead window that were never reminded."""
    moment = ensure_utc(now or datetime.now(UTC))
    stmt = (
        select(Appointment)
        .where(
            Appointment.deleted_at.is_(None),
            Appointment.reminder_sent_at.is_(None),
            Appointment.scheduled_at > moment,
            Appointment.scheduled_at <= moment + timedelta(hours=lead_hours),
            Appointment.status.in_([s.value for s in AppointmentStatus.active()]),
        )
        .order_by(Appointment.scheduled_at.asc())
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all())


async def overdue_without_outcome(
    session: AsyncSession,
    *,
    now: datetime | None = None,
    grace_hours: int = NO_SHOW_GRACE_HOURS,
    limit: int = 500,
) -> list[Appointment]:
    """Bookings whose slot ended long enough ago that staff should have closed them.

    The end of the slot is ``scheduled_at + duration``, which SQL cannot index,
    so the predicate is on ``scheduled_at`` with the longest bookable
    appointment subtracted; the exact end is checked in Python.
    """
    moment = ensure_utc(now or datetime.now(UTC))
    cutoff = moment - timedelta(hours=grace_hours)
    stmt = (
        select(Appointment)
        .where(
            Appointment.deleted_at.is_(None),
            Appointment.scheduled_at <= cutoff,
            Appointment.status.in_([s.value for s in AppointmentStatus.active()]),
        )
        .order_by(Appointment.scheduled_at.asc())
        .limit(limit)
    )
    rows = (await session.execute(stmt)).scalars().all()
    return [
        row
        for row in rows
        if ensure_utc(row.scheduled_at) + timedelta(minutes=row.duration_minutes) <= cutoff
    ]


def reminder_text(appointment: Appointment, business: Business, timezone: str) -> str:
    """The WhatsApp reminder body. Kept short — it is read on a phone."""
    local = ensure_utc(appointment.scheduled_at).astimezone(ZoneInfo(timezone))
    what = f" for {appointment.service}" if appointment.service else ""
    return (
        f"Reminder from {business.name}: your appointment{what} is on "
        f"{local:%A %d %B} at {local:%I:%M %p}. "
        "Reply here to reschedule or cancel."
    )
