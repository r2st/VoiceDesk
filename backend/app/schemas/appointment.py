"""Appointment, availability and business-hours schemas."""

from __future__ import annotations

import uuid
from datetime import date, datetime, time
from typing import Annotated

from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator

from app.models.enums import AppointmentSource, AppointmentStatus, Language
from app.schemas.common import ORMModel, normalize_phone

MAX_DURATION_MINUTES = 480


def require_offset(value: datetime) -> datetime:
    """Reject a naive ``scheduled_at``.

    A wall-clock time without an offset is ambiguous. The dashboard and the
    voice agent both know the business timezone, so making them say which
    instant they mean removes a whole class of "booked at the wrong hour" bugs.
    """
    if value.tzinfo is None:
        raise ValueError("scheduled_at must include a UTC offset, e.g. 2026-08-10T10:00+05:30")
    return value


class AppointmentBase(BaseModel):
    customer_name: Annotated[str, Field(min_length=2, max_length=200)]
    customer_phone: Annotated[str, Field(min_length=6, max_length=20)]
    customer_email: EmailStr | None = None
    service: Annotated[str | None, Field(default=None, max_length=200)] = None
    scheduled_at: datetime
    duration_minutes: Annotated[int, Field(ge=5, le=MAX_DURATION_MINUTES)] = 30
    language: Language | None = None
    notes: Annotated[str | None, Field(default=None, max_length=2000)] = None

    @field_validator("customer_phone")
    @classmethod
    def _normalize_customer_phone(cls, value: str) -> str:
        return normalize_phone(value)

    @field_validator("scheduled_at")
    @classmethod
    def _offset_required(cls, value: datetime) -> datetime:
        return require_offset(value)


class AppointmentCreate(AppointmentBase):
    agent_id: uuid.UUID | None = None
    call_id: uuid.UUID | None = None
    source: AppointmentSource = AppointmentSource.DASHBOARD
    #: Staff booking a walk-in may place it outside published hours; capacity
    #: is still enforced, because double-booking is the harm that matters.
    override_hours: bool = False


class AppointmentUpdate(BaseModel):
    customer_name: Annotated[str | None, Field(default=None, min_length=2, max_length=200)] = None
    customer_phone: Annotated[str | None, Field(default=None, min_length=6, max_length=20)] = None
    customer_email: EmailStr | None = None
    service: Annotated[str | None, Field(default=None, max_length=200)] = None
    language: Language | None = None
    notes: Annotated[str | None, Field(default=None, max_length=2000)] = None

    @field_validator("customer_phone")
    @classmethod
    def _normalize_customer_phone(cls, value: str | None) -> str | None:
        return normalize_phone(value) if value else None


class AppointmentReschedule(BaseModel):
    scheduled_at: datetime
    duration_minutes: Annotated[int | None, Field(default=None, ge=5, le=MAX_DURATION_MINUTES)] = (
        None
    )
    reason: Annotated[str | None, Field(default=None, max_length=500)] = None
    override_hours: bool = False

    @field_validator("scheduled_at")
    @classmethod
    def _offset_required(cls, value: datetime) -> datetime:
        return require_offset(value)


class AppointmentCancel(BaseModel):
    reason: Annotated[str | None, Field(default=None, max_length=500)] = None


class AppointmentStatusUpdate(BaseModel):
    status: AppointmentStatus


class AppointmentOut(ORMModel):
    id: uuid.UUID
    business_id: uuid.UUID
    agent_id: uuid.UUID | None
    call_id: uuid.UUID | None
    customer_name: str
    customer_phone: str
    customer_email: str | None
    service: str | None
    scheduled_at: datetime
    duration_minutes: int
    status: AppointmentStatus
    source: AppointmentSource
    language: Language | None
    notes: str | None
    confirmed_at: datetime | None
    cancelled_at: datetime | None
    cancellation_reason: str | None
    reminder_sent_at: datetime | None
    created_at: datetime
    updated_at: datetime


class SlotOut(BaseModel):
    start: datetime
    end: datetime
    remaining_capacity: int


class AvailabilityOut(BaseModel):
    date: date
    timezone: str
    is_open: bool
    duration_minutes: int
    slots: list[SlotOut]


class BusinessHoursUpdate(BaseModel):
    """Replace the whole weekly schedule. Absent days are treated as closed."""

    hours: dict[str, list[tuple[time, time]]] = Field(default_factory=dict)
    slot_minutes: Annotated[int | None, Field(default=None, ge=5, le=240)] = None
    capacity_per_slot: Annotated[int | None, Field(default=None, ge=1, le=100)] = None
    min_notice_minutes: Annotated[int | None, Field(default=None, ge=0, le=10_080)] = None
    max_advance_days: Annotated[int | None, Field(default=None, ge=1, le=730)] = None
    timezone: Annotated[str | None, Field(default=None, max_length=60)] = None
    closed_dates: list[date] | None = None

    @model_validator(mode="after")
    def _validate_windows(self) -> BusinessHoursUpdate:
        from app.services.scheduling import WEEKDAY_KEYS

        for key, windows in self.hours.items():
            if key not in WEEKDAY_KEYS:
                raise ValueError(f"Unknown weekday '{key}'; use one of {', '.join(WEEKDAY_KEYS)}.")
            for start, end in windows:
                if start >= end:
                    raise ValueError(f"{key}: window {start}-{end} does not end after it starts.")
        return self


class ScheduleConfigOut(BaseModel):
    timezone: str
    slot_minutes: int
    capacity_per_slot: int
    min_notice_minutes: int
    max_advance_days: int
    hours: dict[str, list[tuple[time, time]]]
    closed_dates: list[date]
