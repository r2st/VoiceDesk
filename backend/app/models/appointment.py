"""Appointments booked by a voice agent or by dashboard staff.

Appointment booking is the platform's primary use case (design doc §1), and
``IntentActionType.BOOK_APPOINTMENT`` is the action the conversation engine
raises when a caller asks for a slot. This table is where that action lands.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, SoftDeleteMixin, TimestampMixin, UUIDPrimaryKeyMixin
from app.db.types import GUID, JSONBType
from app.models.enums import AppointmentSource, AppointmentStatus


class Appointment(Base, UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin):
    """One booked slot for one customer."""

    __tablename__ = "appointments"

    business_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: The agent that took the booking, when it came in over the phone.
    agent_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("voice_agents.id", ondelete="SET NULL"), nullable=True, index=True
    )
    #: The call the booking was made on, for auditing a disputed slot.
    call_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("call_logs.id", ondelete="SET NULL"), nullable=True, index=True
    )

    customer_name: Mapped[str] = mapped_column(String(200), nullable=False)
    #: Not indexed on its own — every lookup is tenant-scoped, so the composite
    #: ``(business_id, customer_phone)`` index below is the one that gets used.
    customer_phone: Mapped[str] = mapped_column(String(20), nullable=False)
    customer_email: Mapped[str | None] = mapped_column(String(255), nullable=True)

    #: What the appointment is for, e.g. "Full body checkup".
    service: Mapped[str | None] = mapped_column(String(200), nullable=True)
    #: Always stored in UTC; presented to staff in the business timezone.
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    duration_minutes: Mapped[int] = mapped_column(Integer, default=30, nullable=False)

    status: Mapped[str] = mapped_column(
        String(20), default=AppointmentStatus.SCHEDULED, nullable=False
    )
    source: Mapped[str] = mapped_column(
        String(20), default=AppointmentSource.DASHBOARD, nullable=False
    )
    language: Mapped[str | None] = mapped_column(String(5), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancellation_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    reminder_sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    #: Reschedule history and any structured slots the agent collected.
    metadata_json: Mapped[dict] = mapped_column(JSONBType, default=dict, nullable=False)

    __table_args__ = (
        # The calendar view and the availability check both scan a date range
        # for one tenant, skipping cancelled and deleted rows.
        Index("ix_appointments_business_scheduled", "business_id", "scheduled_at", "deleted_at"),
        Index("ix_appointments_business_status", "business_id", "status", "deleted_at"),
        Index("ix_appointments_business_phone", "business_id", "customer_phone"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Appointment {self.id} {self.scheduled_at:%Y-%m-%d %H:%M} {self.status}>"
