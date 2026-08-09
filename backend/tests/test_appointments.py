"""Appointment booking, availability, lifecycle and tenant isolation."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, NotFoundError, ValidationError
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
from app.services import appointment_service

IST = ZoneInfo("Asia/Kolkata")

#: Monday. The whole module pins "now" to 09:00 IST that morning so booking
#: horizons are deterministic regardless of when the suite runs.
MONDAY = date(2026, 8, 10)
NOW = datetime(2026, 8, 10, 9, 0, tzinfo=IST)


def at(hour: int, minute: int = 0, *, day: date = MONDAY) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=IST)


def make_payload(**overrides) -> AppointmentCreate:
    data = {
        "customer_name": "Asha Menon",
        "customer_phone": "9876500011",
        "scheduled_at": at(11, 0),
        "duration_minutes": 30,
        "service": "Blood test",
    }
    data.update(overrides)
    return AppointmentCreate(**data)


async def book(session: AsyncSession, business: Business, **overrides) -> Appointment:
    return await appointment_service.book(session, business.id, make_payload(**overrides), now=NOW)


# --------------------------------------------------------------------------- #
# Booking
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
class TestBooking:
    async def test_a_slot_inside_opening_hours_is_booked(
        self, session: AsyncSession, business: Business
    ) -> None:
        appointment = await book(session, business)

        assert appointment.status == AppointmentStatus.SCHEDULED
        assert appointment.business_id == business.id
        # Stored in UTC regardless of the offset the caller sent.
        assert appointment.scheduled_at.astimezone(UTC) == at(11, 0).astimezone(UTC)
        assert appointment.customer_phone == "+919876500011"

    async def test_booking_outside_opening_hours_is_refused(
        self, session: AsyncSession, business: Business
    ) -> None:
        with pytest.raises(ValidationError, match="outside the business's opening hours"):
            await book(session, business, scheduled_at=at(22, 0))

    async def test_booking_in_the_lunch_break_is_refused(
        self, session: AsyncSession, business: Business
    ) -> None:
        with pytest.raises(ValidationError, match="opening hours"):
            await book(session, business, scheduled_at=at(13, 30))

    async def test_staff_may_override_hours_for_a_walk_in(
        self, session: AsyncSession, business: Business
    ) -> None:
        appointment = await book(session, business, scheduled_at=at(22, 0), override_hours=True)

        assert appointment.status == AppointmentStatus.SCHEDULED

    async def test_a_time_inside_the_notice_window_is_refused(
        self, session: AsyncSession, business: Business
    ) -> None:
        """Default notice is 30 minutes and "now" is 09:00, so 09:15 is too soon."""
        with pytest.raises(ValidationError, match="too soon"):
            await book(session, business, scheduled_at=at(9, 15))

    async def test_a_time_in_the_past_is_refused(
        self, session: AsyncSession, business: Business
    ) -> None:
        with pytest.raises(ValidationError, match="too soon"):
            await book(session, business, scheduled_at=at(9, 0, day=date(2026, 8, 3)))

    async def test_a_time_beyond_the_booking_horizon_is_refused(
        self, session: AsyncSession, business: Business
    ) -> None:
        far = at(11, 0, day=MONDAY + timedelta(days=400))

        with pytest.raises(ValidationError, match="days ahead"):
            await book(session, business, scheduled_at=far)

    async def test_a_second_booking_in_a_full_slot_is_rejected(
        self, session: AsyncSession, business: Business
    ) -> None:
        await book(session, business)

        with pytest.raises(ConflictError, match="fully booked"):
            await book(session, business, customer_name="Ravi Kumar")

    async def test_an_overlapping_longer_booking_is_rejected(
        self, session: AsyncSession, business: Business
    ) -> None:
        await book(session, business, scheduled_at=at(11, 0), duration_minutes=60)

        with pytest.raises(ConflictError, match="fully booked"):
            await book(session, business, scheduled_at=at(11, 30))

    async def test_a_back_to_back_booking_does_not_overlap(
        self, session: AsyncSession, business: Business
    ) -> None:
        await book(session, business, scheduled_at=at(11, 0), duration_minutes=30)

        later = await book(session, business, scheduled_at=at(11, 30))

        assert later.status == AppointmentStatus.SCHEDULED

    async def test_capacity_above_one_allows_concurrent_bookings(
        self, session: AsyncSession, business: Business
    ) -> None:
        business.settings_json = {"appointments": {"capacity_per_slot": 2}}
        await session.flush()

        await book(session, business)
        second = await book(session, business, customer_name="Ravi Kumar")

        assert second.status == AppointmentStatus.SCHEDULED
        with pytest.raises(ConflictError):
            await book(session, business, customer_name="Third Person")

    async def test_a_cancelled_booking_frees_its_slot(
        self, session: AsyncSession, business: Business
    ) -> None:
        first = await book(session, business)
        await appointment_service.cancel(session, business.id, first.id, "Customer called off")

        replacement = await book(session, business, customer_name="Ravi Kumar")

        assert replacement.id != first.id

    async def test_booking_against_another_tenants_agent_is_a_404(
        self, session: AsyncSession, business: Business, other_business: Business
    ) -> None:
        foreign = VoiceAgent(
            business_id=other_business.id, name="Rival Agent", persona="", flow_json={}
        )
        session.add(foreign)
        await session.flush()

        with pytest.raises(NotFoundError):
            await book(session, business, agent_id=foreign.id)

    async def test_the_booking_lock_is_released_after_a_successful_booking(
        self, session: AsyncSession, business: Business
    ) -> None:
        """A leaked lock would wedge the whole day for other bookings."""
        await book(session, business)

        second = await book(session, business, scheduled_at=at(12, 0))

        assert second.status == AppointmentStatus.SCHEDULED

    async def test_the_booking_lock_is_released_after_a_rejected_booking(
        self, session: AsyncSession, business: Business
    ) -> None:
        await book(session, business)
        with pytest.raises(ConflictError):
            await book(session, business, customer_name="Ravi Kumar")

        later = await book(session, business, scheduled_at=at(12, 0))

        assert later.status == AppointmentStatus.SCHEDULED


@pytest.mark.asyncio
class TestAvailability:
    async def test_slots_cover_both_windows_of_the_working_day(
        self, session: AsyncSession, business: Business
    ) -> None:
        slots, config = await appointment_service.available_slots(
            session, business.id, MONDAY, now=NOW
        )

        assert config.timezone == "Asia/Kolkata"
        starts = {s.start.astimezone(IST).strftime("%H:%M") for s in slots}
        assert "10:00" in starts
        assert "14:00" in starts

    async def test_slots_before_the_notice_cutoff_are_hidden(
        self, session: AsyncSession, business: Business
    ) -> None:
        slots, _ = await appointment_service.available_slots(session, business.id, MONDAY, now=NOW)

        starts = {s.start.astimezone(IST).strftime("%H:%M") for s in slots}
        assert "09:00" not in starts
        assert "09:30" in starts

    async def test_a_booked_slot_disappears_from_availability(
        self, session: AsyncSession, business: Business
    ) -> None:
        await book(session, business, scheduled_at=at(11, 0))

        slots, _ = await appointment_service.available_slots(session, business.id, MONDAY, now=NOW)

        starts = {s.start.astimezone(IST).strftime("%H:%M") for s in slots}
        assert "11:00" not in starts
        assert "11:30" in starts

    async def test_a_long_booking_hides_every_slot_it_covers(
        self, session: AsyncSession, business: Business
    ) -> None:
        await book(session, business, scheduled_at=at(11, 0), duration_minutes=90)

        slots, _ = await appointment_service.available_slots(session, business.id, MONDAY, now=NOW)

        starts = {s.start.astimezone(IST).strftime("%H:%M") for s in slots}
        assert {"11:00", "11:30", "12:00"}.isdisjoint(starts)
        assert "12:30" in starts

    async def test_remaining_capacity_is_reported(
        self, session: AsyncSession, business: Business
    ) -> None:
        business.settings_json = {"appointments": {"capacity_per_slot": 3}}
        await session.flush()
        await book(session, business, scheduled_at=at(11, 0))

        slots, _ = await appointment_service.available_slots(session, business.id, MONDAY, now=NOW)

        eleven = next(s for s in slots if s.start.astimezone(IST).hour == 11)
        assert eleven.remaining_capacity == 2

    async def test_a_closed_day_offers_nothing(
        self, session: AsyncSession, business: Business
    ) -> None:
        slots, _ = await appointment_service.available_slots(
            session, business.id, date(2026, 8, 16), now=NOW
        )

        assert slots == []

    async def test_a_day_past_the_horizon_offers_nothing(
        self, session: AsyncSession, business: Business
    ) -> None:
        slots, _ = await appointment_service.available_slots(
            session, business.id, MONDAY + timedelta(days=400), now=NOW
        )

        assert slots == []

    async def test_next_available_skips_closed_days_and_full_slots(
        self, session: AsyncSession, business: Business
    ) -> None:
        business.settings_json = {"business_hours": {"mon": [], "tue": [["09:00", "10:00"]]}}
        await session.flush()

        slots = await appointment_service.next_available_slots(
            session, business.id, limit=2, now=NOW
        )

        assert [s.start.astimezone(IST).strftime("%a %H:%M") for s in slots] == [
            "Tue 09:00",
            "Tue 09:30",
        ]

    async def test_next_available_reflects_existing_bookings(
        self, session: AsyncSession, business: Business
    ) -> None:
        await book(session, business, scheduled_at=at(9, 30))

        slots = await appointment_service.next_available_slots(
            session, business.id, limit=1, now=NOW
        )

        assert slots[0].start.astimezone(IST).strftime("%H:%M") == "10:00"


@pytest.mark.asyncio
class TestLifecycle:
    async def test_rescheduling_moves_the_slot_and_records_history(
        self, session: AsyncSession, business: Business
    ) -> None:
        appointment = await book(session, business)

        moved = await appointment_service.reschedule(
            session,
            business.id,
            appointment.id,
            AppointmentReschedule(scheduled_at=at(15, 0), reason="Customer request"),
            now=NOW,
        )

        assert moved.scheduled_at.astimezone(IST).hour == 15
        history = moved.metadata_json["reschedule_history"]
        assert len(history) == 1
        assert history[0]["reason"] == "Customer request"

    async def test_rescheduling_clears_confirmation_and_reminder_state(
        self, session: AsyncSession, business: Business
    ) -> None:
        appointment = await book(session, business)
        await appointment_service.set_status(
            session, business.id, appointment.id, AppointmentStatus.CONFIRMED
        )
        appointment.reminder_sent_at = datetime.now(UTC)
        await session.flush()

        moved = await appointment_service.reschedule(
            session,
            business.id,
            appointment.id,
            AppointmentReschedule(scheduled_at=at(15, 0)),
            now=NOW,
        )

        assert moved.status == AppointmentStatus.SCHEDULED
        assert moved.confirmed_at is None
        assert moved.reminder_sent_at is None

    async def test_rescheduling_onto_a_full_slot_is_rejected(
        self, session: AsyncSession, business: Business
    ) -> None:
        first = await book(session, business, scheduled_at=at(11, 0))
        second = await book(session, business, scheduled_at=at(15, 0), customer_name="Ravi Kumar")

        with pytest.raises(ConflictError):
            await appointment_service.reschedule(
                session,
                business.id,
                second.id,
                AppointmentReschedule(scheduled_at=at(11, 0)),
                now=NOW,
            )
        assert first.scheduled_at.astimezone(IST).hour == 11

    async def test_rescheduling_onto_its_own_slot_is_allowed(
        self, session: AsyncSession, business: Business
    ) -> None:
        """The appointment must not collide with itself when only length changes."""
        appointment = await book(session, business, scheduled_at=at(11, 0))

        moved = await appointment_service.reschedule(
            session,
            business.id,
            appointment.id,
            AppointmentReschedule(scheduled_at=at(11, 0), duration_minutes=60),
            now=NOW,
        )

        assert moved.duration_minutes == 60

    async def test_a_cancelled_appointment_cannot_be_rescheduled(
        self, session: AsyncSession, business: Business
    ) -> None:
        appointment = await book(session, business)
        await appointment_service.cancel(session, business.id, appointment.id)

        with pytest.raises(ValidationError, match="cannot be rescheduled"):
            await appointment_service.reschedule(
                session,
                business.id,
                appointment.id,
                AppointmentReschedule(scheduled_at=at(15, 0)),
                now=NOW,
            )

    async def test_cancelling_records_the_reason_and_is_idempotent(
        self, session: AsyncSession, business: Business
    ) -> None:
        appointment = await book(session, business)

        cancelled = await appointment_service.cancel(
            session, business.id, appointment.id, "Doctor unavailable"
        )
        again = await appointment_service.cancel(session, business.id, appointment.id, "ignored")

        assert cancelled.status == AppointmentStatus.CANCELLED
        assert cancelled.cancelled_at is not None
        assert again.cancellation_reason == "Doctor unavailable"

    @pytest.mark.parametrize(
        "target",
        [AppointmentStatus.CONFIRMED, AppointmentStatus.COMPLETED, AppointmentStatus.NO_SHOW],
    )
    async def test_permitted_status_transitions_are_applied(
        self, session: AsyncSession, business: Business, target: AppointmentStatus
    ) -> None:
        appointment = await book(session, business)

        updated = await appointment_service.set_status(session, business.id, appointment.id, target)

        assert updated.status == target

    async def test_confirming_stamps_confirmed_at(
        self, session: AsyncSession, business: Business
    ) -> None:
        appointment = await book(session, business)

        updated = await appointment_service.set_status(
            session, business.id, appointment.id, AppointmentStatus.CONFIRMED
        )

        assert updated.confirmed_at is not None

    async def test_a_completed_appointment_cannot_be_reopened(
        self, session: AsyncSession, business: Business
    ) -> None:
        appointment = await book(session, business)
        await appointment_service.set_status(
            session, business.id, appointment.id, AppointmentStatus.COMPLETED
        )

        with pytest.raises(ValidationError, match="cannot move"):
            await appointment_service.set_status(
                session, business.id, appointment.id, AppointmentStatus.SCHEDULED
            )

    async def test_updating_details_leaves_the_slot_alone(
        self, session: AsyncSession, business: Business
    ) -> None:
        appointment = await book(session, business)
        original = appointment.scheduled_at

        updated = await appointment_service.update_details(
            session,
            business.id,
            appointment.id,
            AppointmentUpdate(customer_name="Asha M.", notes="Bring old reports"),
        )

        assert updated.customer_name == "Asha M."
        assert updated.notes == "Bring old reports"
        assert updated.scheduled_at == original

    async def test_a_soft_deleted_appointment_is_gone_and_frees_its_slot(
        self, session: AsyncSession, business: Business
    ) -> None:
        appointment = await book(session, business)

        await appointment_service.soft_delete(session, business.id, appointment.id)

        with pytest.raises(NotFoundError):
            await appointment_service.get_appointment(session, business.id, appointment.id)
        replacement = await book(session, business, customer_name="Ravi Kumar")
        assert replacement.id != appointment.id


@pytest.mark.asyncio
class TestQueries:
    async def test_listing_filters_by_status_and_orders_by_time(
        self, session: AsyncSession, business: Business
    ) -> None:
        late = await book(session, business, scheduled_at=at(15, 0))
        early = await book(session, business, scheduled_at=at(11, 0), customer_name="Ravi Kumar")
        await appointment_service.cancel(session, business.id, late.id)

        active, total = await appointment_service.list_appointments(
            session, business.id, status=AppointmentStatus.SCHEDULED
        )

        assert total == 1
        assert [a.id for a in active] == [early.id]

    async def test_listing_filters_by_date_range(
        self, session: AsyncSession, business: Business
    ) -> None:
        await book(session, business, scheduled_at=at(11, 0))
        tuesday = await book(
            session, business, scheduled_at=at(11, 0, day=MONDAY + timedelta(days=1))
        )

        rows, total = await appointment_service.list_appointments(
            session,
            business.id,
            starts_after=at(0, 0, day=MONDAY + timedelta(days=1)),
        )

        assert total == 1
        assert [a.id for a in rows] == [tuesday.id]

    async def test_search_matches_name_phone_and_service(
        self, session: AsyncSession, business: Business
    ) -> None:
        await book(session, business, customer_name="Asha Menon")
        await book(session, business, scheduled_at=at(15, 0), customer_name="Ravi Kumar")

        rows, total = await appointment_service.list_appointments(
            session, business.id, search="Ravi"
        )

        assert total == 1
        assert rows[0].customer_name == "Ravi Kumar"

    async def test_upcoming_for_phone_ignores_past_and_cancelled(
        self, session: AsyncSession, business: Business
    ) -> None:
        upcoming = await book(session, business, scheduled_at=at(15, 0))
        cancelled = await book(session, business, scheduled_at=at(16, 0))
        await appointment_service.cancel(session, business.id, cancelled.id)

        rows = await appointment_service.upcoming_for_phone(
            session, business.id, "+919876500011", now=at(12, 0)
        )

        assert [a.id for a in rows] == [upcoming.id]

    async def test_another_tenants_appointments_are_invisible(
        self,
        session: AsyncSession,
        business: Business,
        other_business: Business,
    ) -> None:
        mine = await book(session, business)
        theirs = await appointment_service.book(
            session, other_business.id, make_payload(scheduled_at=at(15, 0)), now=NOW
        )

        rows, total = await appointment_service.list_appointments(session, business.id)

        assert total == 1
        assert [a.id for a in rows] == [mine.id]
        with pytest.raises(NotFoundError):
            await appointment_service.get_appointment(session, business.id, theirs.id)

    async def test_one_tenants_bookings_do_not_consume_anothers_capacity(
        self, session: AsyncSession, business: Business, other_business: Business
    ) -> None:
        await appointment_service.book(session, other_business.id, make_payload(), now=NOW)

        mine = await book(session, business)

        assert mine.status == AppointmentStatus.SCHEDULED


@pytest.mark.asyncio
class TestScheduleSettings:
    async def test_saving_hours_round_trips_through_business_settings(
        self, session: AsyncSession, business: Business
    ) -> None:
        from datetime import time

        config = await appointment_service.update_schedule_config(
            session,
            business.id,
            BusinessHoursUpdate(
                hours={"mon": [(time(10, 0), time(16, 0))], "sun": []},
                slot_minutes=60,
                capacity_per_slot=2,
            ),
        )

        assert config.slot_minutes == 60
        assert config.capacity_per_slot == 2
        assert config.windows_for(MONDAY) == ((time(10, 0), time(16, 0)),)
        assert business.settings_json["business_hours"]["mon"] == [["10:00", "16:00"]]

    async def test_saved_hours_govern_subsequent_bookings(
        self, session: AsyncSession, business: Business
    ) -> None:
        from datetime import time

        await appointment_service.update_schedule_config(
            session,
            business.id,
            BusinessHoursUpdate(hours={"mon": [(time(10, 0), time(11, 0))]}),
        )

        with pytest.raises(ValidationError, match="opening hours"):
            await book(session, business, scheduled_at=at(14, 0))

    async def test_updating_settings_preserves_unrelated_keys(
        self, session: AsyncSession, business: Business
    ) -> None:
        business.settings_json = {"escalation_contact": "+919000000000"}
        await session.flush()

        await appointment_service.update_schedule_config(
            session, business.id, BusinessHoursUpdate(slot_minutes=15)
        )

        assert business.settings_json["escalation_contact"] == "+919000000000"
        assert business.settings_json["appointments"]["slot_minutes"] == 15


# --------------------------------------------------------------------------- #
# HTTP surface
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
class TestAppointmentEndpoints:
    async def test_create_and_fetch(self, client: AsyncClient, owner_headers: dict) -> None:
        response = await client.post(
            "/api/v1/appointments",
            headers=owner_headers,
            json={
                "customer_name": "Asha Menon",
                "customer_phone": "9876500011",
                "scheduled_at": _future(11).isoformat(),
                "service": "Blood test",
            },
        )

        assert response.status_code == 201, response.text
        body = response.json()
        assert body["status"] == "scheduled"
        assert body["customer_phone"] == "+919876500011"

        fetched = await client.get(f"/api/v1/appointments/{body['id']}", headers=owner_headers)
        assert fetched.status_code == 200
        assert fetched.json()["id"] == body["id"]

    async def test_a_naive_scheduled_at_is_rejected(
        self, client: AsyncClient, owner_headers: dict
    ) -> None:
        response = await client.post(
            "/api/v1/appointments",
            headers=owner_headers,
            json={
                "customer_name": "Asha Menon",
                "customer_phone": "9876500011",
                "scheduled_at": _future(11).replace(tzinfo=None).isoformat(),
            },
        )

        assert response.status_code == 422
        assert "UTC offset" in response.text

    async def test_availability_lists_open_slots(
        self, client: AsyncClient, owner_headers: dict
    ) -> None:
        day = _future(11).astimezone(IST).date()

        response = await client.get(
            "/api/v1/appointments/availability",
            headers=owner_headers,
            params={"date": day.isoformat()},
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["timezone"] == "Asia/Kolkata"
        assert body["is_open"] is True
        assert len(body["slots"]) > 0

    async def test_availability_is_not_parsed_as_an_appointment_id(
        self, client: AsyncClient, owner_headers: dict
    ) -> None:
        """``/availability`` must win over ``/{appointment_id}``."""
        response = await client.get(
            "/api/v1/appointments/availability",
            headers=owner_headers,
            params={"date": _future(11).astimezone(IST).date().isoformat()},
        )

        assert response.status_code == 200

    async def test_double_booking_returns_409(
        self, client: AsyncClient, owner_headers: dict
    ) -> None:
        payload = {
            "customer_name": "Asha Menon",
            "customer_phone": "9876500011",
            "scheduled_at": _future(11).isoformat(),
        }
        first = await client.post("/api/v1/appointments", headers=owner_headers, json=payload)
        assert first.status_code == 201

        second = await client.post(
            "/api/v1/appointments",
            headers=owner_headers,
            json={**payload, "customer_name": "Ravi Kumar"},
        )

        assert second.status_code == 409
        assert second.json()["error"]["code"] == "conflict"

    async def test_reschedule_cancel_and_status_endpoints(
        self, client: AsyncClient, owner_headers: dict
    ) -> None:
        created = await client.post(
            "/api/v1/appointments",
            headers=owner_headers,
            json={
                "customer_name": "Asha Menon",
                "customer_phone": "9876500011",
                "scheduled_at": _future(11).isoformat(),
            },
        )
        appointment_id = created.json()["id"]

        moved = await client.post(
            f"/api/v1/appointments/{appointment_id}/reschedule",
            headers=owner_headers,
            json={"scheduled_at": _future(15).isoformat(), "reason": "Customer request"},
        )
        assert moved.status_code == 200, moved.text

        confirmed = await client.post(
            f"/api/v1/appointments/{appointment_id}/status",
            headers=owner_headers,
            json={"status": "confirmed"},
        )
        assert confirmed.json()["status"] == "confirmed"

        cancelled = await client.post(
            f"/api/v1/appointments/{appointment_id}/cancel",
            headers=owner_headers,
            json={"reason": "Doctor unavailable"},
        )
        assert cancelled.json()["status"] == "cancelled"
        assert cancelled.json()["cancellation_reason"] == "Doctor unavailable"

    async def test_a_viewer_may_read_but_not_book(
        self, client: AsyncClient, viewer_headers: dict
    ) -> None:
        listing = await client.get("/api/v1/appointments", headers=viewer_headers)
        assert listing.status_code == 200

        response = await client.post(
            "/api/v1/appointments",
            headers=viewer_headers,
            json={
                "customer_name": "Asha Menon",
                "customer_phone": "9876500011",
                "scheduled_at": _future(11).isoformat(),
            },
        )

        assert response.status_code == 403

    async def test_only_admins_may_delete(
        self, client: AsyncClient, owner_headers: dict, viewer_headers: dict
    ) -> None:
        created = await client.post(
            "/api/v1/appointments",
            headers=owner_headers,
            json={
                "customer_name": "Asha Menon",
                "customer_phone": "9876500011",
                "scheduled_at": _future(11).isoformat(),
            },
        )
        appointment_id = created.json()["id"]

        denied = await client.delete(
            f"/api/v1/appointments/{appointment_id}", headers=viewer_headers
        )
        assert denied.status_code == 403

        allowed = await client.delete(
            f"/api/v1/appointments/{appointment_id}", headers=owner_headers
        )
        assert allowed.status_code == 204

    async def test_another_tenant_cannot_read_the_appointment(
        self, client: AsyncClient, owner_headers: dict, other_headers: dict
    ) -> None:
        created = await client.post(
            "/api/v1/appointments",
            headers=owner_headers,
            json={
                "customer_name": "Asha Menon",
                "customer_phone": "9876500011",
                "scheduled_at": _future(11).isoformat(),
            },
        )
        appointment_id = created.json()["id"]

        response = await client.get(f"/api/v1/appointments/{appointment_id}", headers=other_headers)

        assert response.status_code == 404

    async def test_anonymous_access_is_rejected(self, client: AsyncClient) -> None:
        response = await client.get("/api/v1/appointments")

        assert response.status_code == 401

    async def test_schedule_settings_round_trip_over_http(
        self, client: AsyncClient, owner_headers: dict
    ) -> None:
        response = await client.put(
            "/api/v1/appointments/schedule",
            headers=owner_headers,
            json={
                "hours": {"mon": [["10:00", "16:00"]], "sun": []},
                "slot_minutes": 60,
                "capacity_per_slot": 2,
            },
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["slot_minutes"] == 60
        assert body["hours"]["mon"] == [["10:00:00", "16:00:00"]]

        fetched = await client.get("/api/v1/appointments/schedule", headers=owner_headers)
        assert fetched.json()["capacity_per_slot"] == 2

    async def test_an_unknown_weekday_is_rejected(
        self, client: AsyncClient, owner_headers: dict
    ) -> None:
        response = await client.put(
            "/api/v1/appointments/schedule",
            headers=owner_headers,
            json={"hours": {"funday": [["10:00", "16:00"]]}},
        )

        assert response.status_code == 422

    async def test_a_viewer_may_not_change_the_schedule(
        self, client: AsyncClient, viewer_headers: dict
    ) -> None:
        response = await client.put(
            "/api/v1/appointments/schedule",
            headers=viewer_headers,
            json={"slot_minutes": 60},
        )

        assert response.status_code == 403

    async def test_next_available_returns_slots(
        self, client: AsyncClient, owner_headers: dict
    ) -> None:
        response = await client.get(
            "/api/v1/appointments/next-available", headers=owner_headers, params={"limit": 2}
        )

        assert response.status_code == 200
        assert len(response.json()) == 2

    async def test_an_unknown_appointment_is_a_404(
        self, client: AsyncClient, owner_headers: dict
    ) -> None:
        response = await client.get(f"/api/v1/appointments/{uuid.uuid4()}", headers=owner_headers)

        assert response.status_code == 404


def _future(hour: int) -> datetime:
    """The next weekday at ``hour`` IST, comfortably past the notice window.

    The HTTP tests run against the real clock (the service only accepts an
    injected ``now`` internally), so they need a time that is always bookable.
    """
    candidate = datetime.now(IST).replace(hour=hour, minute=0, second=0, microsecond=0)
    candidate += timedelta(days=1)
    while candidate.weekday() == 6:  # Sunday is closed by default
        candidate += timedelta(days=1)
    return candidate
