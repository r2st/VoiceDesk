"""``/api/v1/appointments`` — bookings, availability and the tenant calendar."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Annotated

from fastapi import APIRouter, Query, Response, status

from app.core.deps import CurrentContext, DbSession, RequireAdmin, RequireOperator
from app.models.enums import AppointmentStatus
from app.schemas.appointment import (
    AppointmentCancel,
    AppointmentCreate,
    AppointmentOut,
    AppointmentReschedule,
    AppointmentStatusUpdate,
    AppointmentUpdate,
    AvailabilityOut,
    BusinessHoursUpdate,
    ScheduleConfigOut,
    SlotOut,
)
from app.schemas.common import Page, normalize_phone
from app.services import appointment_service
from app.services.scheduling import WEEKDAY_KEYS, ScheduleConfig

router = APIRouter(prefix="/appointments", tags=["appointments"])


# Static paths are declared before ``/{appointment_id}`` so that "availability"
# and "schedule" are not parsed as UUIDs.
@router.get("/availability", response_model=AvailabilityOut)
async def get_availability(
    context: CurrentContext,
    session: DbSession,
    day: Annotated[date, Query(alias="date")],
    duration_minutes: Annotated[int | None, Query(ge=5, le=480)] = None,
) -> AvailabilityOut:
    """Bookable slots on one day, already filtered by capacity and notice."""
    slots, config = await appointment_service.available_slots(
        session, context.business_id, day, duration_minutes=duration_minutes
    )
    return AvailabilityOut(
        date=day,
        timezone=config.timezone,
        is_open=config.is_open_on(day),
        duration_minutes=duration_minutes or config.slot_minutes,
        slots=[
            SlotOut(start=s.start, end=s.end, remaining_capacity=s.remaining_capacity)
            for s in slots
        ],
    )


@router.get("/next-available", response_model=list[SlotOut])
async def get_next_available(
    context: CurrentContext,
    session: DbSession,
    limit: Annotated[int, Query(ge=1, le=20)] = 3,
    duration_minutes: Annotated[int | None, Query(ge=5, le=480)] = None,
    search_days: Annotated[int, Query(ge=1, le=90)] = 14,
) -> list[SlotOut]:
    """The soonest openings — what the agent offers when a slot is taken."""
    slots = await appointment_service.next_available_slots(
        session,
        context.business_id,
        limit=limit,
        duration_minutes=duration_minutes,
        search_days=search_days,
    )
    return [
        SlotOut(start=s.start, end=s.end, remaining_capacity=s.remaining_capacity) for s in slots
    ]


@router.get("/schedule", response_model=ScheduleConfigOut)
async def get_schedule(context: CurrentContext, session: DbSession) -> ScheduleConfigOut:
    config = await appointment_service.get_config(session, context.business_id)
    return _config_out(config)


@router.put("/schedule", response_model=ScheduleConfigOut)
async def update_schedule(
    payload: BusinessHoursUpdate, context: RequireAdmin, session: DbSession
) -> ScheduleConfigOut:
    config = await appointment_service.update_schedule_config(session, context.business_id, payload)
    return _config_out(config)


@router.post("", response_model=AppointmentOut, status_code=status.HTTP_201_CREATED)
async def create_appointment(
    payload: AppointmentCreate, context: RequireOperator, session: DbSession
) -> AppointmentOut:
    appointment = await appointment_service.book(session, context.business_id, payload)
    return AppointmentOut.model_validate(appointment)


@router.get("", response_model=Page[AppointmentOut])
async def list_appointments(
    context: CurrentContext,
    session: DbSession,
    status_filter: Annotated[AppointmentStatus | None, Query(alias="status")] = None,
    agent_id: Annotated[uuid.UUID | None, Query()] = None,
    customer_phone: Annotated[str | None, Query(max_length=20)] = None,
    starts_after: Annotated[datetime | None, Query()] = None,
    starts_before: Annotated[datetime | None, Query()] = None,
    search: Annotated[str | None, Query(max_length=120)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[AppointmentOut]:
    appointments, total = await appointment_service.list_appointments(
        session,
        context.business_id,
        status=status_filter,
        agent_id=agent_id,
        customer_phone=normalize_phone(customer_phone) if customer_phone else None,
        starts_after=starts_after,
        starts_before=starts_before,
        search=search,
        limit=limit,
        offset=offset,
    )
    return Page[AppointmentOut](
        items=[AppointmentOut.model_validate(a) for a in appointments],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/{appointment_id}", response_model=AppointmentOut)
async def get_appointment(
    appointment_id: uuid.UUID, context: CurrentContext, session: DbSession
) -> AppointmentOut:
    appointment = await appointment_service.get_appointment(
        session, context.business_id, appointment_id
    )
    return AppointmentOut.model_validate(appointment)


@router.patch("/{appointment_id}", response_model=AppointmentOut)
async def update_appointment(
    appointment_id: uuid.UUID,
    payload: AppointmentUpdate,
    context: RequireOperator,
    session: DbSession,
) -> AppointmentOut:
    appointment = await appointment_service.update_details(
        session, context.business_id, appointment_id, payload
    )
    return AppointmentOut.model_validate(appointment)


@router.post("/{appointment_id}/reschedule", response_model=AppointmentOut)
async def reschedule_appointment(
    appointment_id: uuid.UUID,
    payload: AppointmentReschedule,
    context: RequireOperator,
    session: DbSession,
) -> AppointmentOut:
    appointment = await appointment_service.reschedule(
        session, context.business_id, appointment_id, payload
    )
    return AppointmentOut.model_validate(appointment)


@router.post("/{appointment_id}/cancel", response_model=AppointmentOut)
async def cancel_appointment(
    appointment_id: uuid.UUID,
    payload: AppointmentCancel,
    context: RequireOperator,
    session: DbSession,
) -> AppointmentOut:
    appointment = await appointment_service.cancel(
        session, context.business_id, appointment_id, payload.reason
    )
    return AppointmentOut.model_validate(appointment)


@router.post("/{appointment_id}/status", response_model=AppointmentOut)
async def set_appointment_status(
    appointment_id: uuid.UUID,
    payload: AppointmentStatusUpdate,
    context: RequireOperator,
    session: DbSession,
) -> AppointmentOut:
    appointment = await appointment_service.set_status(
        session, context.business_id, appointment_id, payload.status
    )
    return AppointmentOut.model_validate(appointment)


@router.delete("/{appointment_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_appointment(
    appointment_id: uuid.UUID, context: RequireAdmin, session: DbSession
) -> Response:
    """Soft delete — the row is retained with ``deleted_at`` set."""
    await appointment_service.soft_delete(session, context.business_id, appointment_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _config_out(config: ScheduleConfig) -> ScheduleConfigOut:
    return ScheduleConfigOut(
        timezone=config.timezone,
        slot_minutes=config.slot_minutes,
        capacity_per_slot=config.capacity_per_slot,
        min_notice_minutes=config.min_notice_minutes,
        max_advance_days=config.max_advance_days,
        hours={key: list(config.hours.get(key, ())) for key in WEEKDAY_KEYS},
        closed_dates=sorted(config.closed_dates),
    )
