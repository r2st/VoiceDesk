"""Booking an appointment from inside a live call.

Two layers: the natural-language time parsing, and the ``book_appointment``
flow node that turns a parsed time into a row in the tenant's calendar.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.timeutil import to_zone
from app.models.appointment import Appointment
from app.models.business import Business
from app.models.call import CallLog
from app.models.enums import (
    AppointmentSource,
    AppointmentStatus,
    CallDirection,
    Language,
)
from app.models.voice_agent import VoiceAgent
from app.services import appointment_service, nlu
from app.services.conversation_engine import ConversationEngine
from app.services.flow import appointment_booking_flow, validate_flow
from tests.fakes import FakeLLMClient

IST = ZoneInfo("Asia/Kolkata")

#: A Monday at 09:00 IST — the reference "now" for every parsing case.
REFERENCE = datetime(2026, 8, 10, 9, 0, tzinfo=IST)


# --------------------------------------------------------------------------- #
# Natural-language time parsing
# --------------------------------------------------------------------------- #
class TestParseDatetimeHeuristic:
    @pytest.mark.parametrize(
        ("phrase", "expected"),
        [
            ("tomorrow at 11 am", "2026-08-11 11:00"),
            ("kal 11 baje", "2026-08-11 11:00"),
            ("कल सुबह 11 बजे", "2026-08-11 11:00"),
            ("today at 3 pm", "2026-08-10 15:00"),
            ("aaj shaam 5 baje", "2026-08-10 17:00"),
            ("day after tomorrow at 10:30", "2026-08-12 10:30"),
            ("parso 4 baje", "2026-08-12 16:00"),
            ("friday at 2 pm", "2026-08-14 14:00"),
            ("shukravar 11 baje", "2026-08-14 11:00"),
        ],
    )
    def test_common_phrasings_resolve(self, phrase: str, expected: str) -> None:
        guess = nlu.parse_datetime_heuristic(phrase, reference=REFERENCE)

        assert guess.when is not None
        assert guess.when.astimezone(IST).strftime("%Y-%m-%d %H:%M") == expected

    def test_a_bare_low_hour_means_the_afternoon(self) -> None:
        """ "Come at 3" on a business call is 15:00, never 03:00."""
        guess = nlu.parse_datetime_heuristic("3 baje", reference=REFERENCE)

        assert guess.when is not None
        assert guess.when.astimezone(IST).hour == 15

    def test_a_morning_marker_keeps_the_hour_as_stated(self) -> None:
        guess = nlu.parse_datetime_heuristic("subah 7 baje", reference=REFERENCE)

        assert guess.when is not None
        assert guess.when.astimezone(IST).hour == 7

    def test_a_time_already_past_today_rolls_to_tomorrow(self) -> None:
        """ "10 baje" said at 18:00 means tomorrow morning, not four hours ago."""
        evening = REFERENCE.replace(hour=18)

        guess = nlu.parse_datetime_heuristic("10 baje", reference=evening)

        assert guess.when is not None
        assert guess.when.astimezone(IST).strftime("%Y-%m-%d %H:%M") == "2026-08-11 10:00"

    def test_an_explicit_day_is_never_rolled_forward(self) -> None:
        guess = nlu.parse_datetime_heuristic("aaj 10 baje", reference=REFERENCE.replace(hour=18))

        assert guess.when is not None
        assert guess.when.astimezone(IST).date().isoformat() == "2026-08-10"

    def test_naming_today_s_weekday_means_next_week(self) -> None:
        """Monday said on a Monday means the next one, not the one already here."""
        guess = nlu.parse_datetime_heuristic("monday at 11 am", reference=REFERENCE)

        assert guess.when is not None
        assert guess.when.astimezone(IST).date().isoformat() == "2026-08-17"

    @pytest.mark.parametrize("phrase", ["", "tomorrow", "kal", "sometime next week", "hello"])
    def test_a_phrase_without_a_time_is_unresolved(self, phrase: str) -> None:
        """A day alone is not bookable — the caller has to be asked for an hour."""
        assert not nlu.parse_datetime_heuristic(phrase, reference=REFERENCE).resolved

    def test_a_phone_number_is_not_read_as_a_time(self) -> None:
        guess = nlu.parse_datetime_heuristic("my number is 9876543210", reference=REFERENCE)

        assert not guess.resolved


@pytest.mark.asyncio
class TestParseDatetimePhrase:
    async def test_the_heuristic_short_circuits_the_model(self, fake_llm: FakeLLMClient) -> None:
        guess = await nlu.parse_datetime_phrase("kal 11 baje", reference=REFERENCE, client=fake_llm)

        assert guess.method.startswith("relative")
        assert fake_llm.calls == []

    async def test_the_model_resolves_what_the_regex_cannot(self, fake_llm: FakeLLMClient) -> None:
        fake_llm.queue_json({"date": "2026-08-13", "time": "16:30", "confidence": 0.8})

        guess = await nlu.parse_datetime_phrase(
            "the Thursday after next, late afternoon", reference=REFERENCE, client=fake_llm
        )

        assert guess.when is not None
        assert guess.when.astimezone(IST).strftime("%Y-%m-%d %H:%M") == "2026-08-13 16:30"
        assert guess.method == "llm"

    async def test_an_incomplete_model_answer_stays_unresolved(
        self, fake_llm: FakeLLMClient
    ) -> None:
        """Rather than invent an hour the caller never said."""
        fake_llm.queue_json({"date": "2026-08-13", "time": None})

        guess = await nlu.parse_datetime_phrase(
            "sometime next week", reference=REFERENCE, client=fake_llm
        )

        assert not guess.resolved
        assert guess.method == "llm-incomplete"

    async def test_a_model_failure_is_absorbed(self, fake_llm: FakeLLMClient) -> None:
        fake_llm.raise_error = True

        guess = await nlu.parse_datetime_phrase(
            "whenever suits you", reference=REFERENCE, client=fake_llm
        )

        assert not guess.resolved
        assert guess.method == "error"


# --------------------------------------------------------------------------- #
# The book_appointment flow node
# --------------------------------------------------------------------------- #
def booking_agent(business: Business) -> VoiceAgent:
    return VoiceAgent(
        business_id=business.id,
        name="Booking Agent",
        use_case="appointment_booking",
        status="active",
        language=Language.ENGLISH,
        persona="You book appointments for a diagnostics clinic.",
        greeting="Sunrise Diagnostics, how can I help?",
        flow_json=appointment_booking_flow("Sunrise Diagnostics, how can I help?"),
        # Off, so a low-confidence turn does not pre-empt the booking assertions.
        whatsapp_handoff_enabled=False,
    )


def next_open_phrase(hour: int = 11) -> tuple[str, datetime]:
    """A phrase for the next non-Sunday at ``hour``, plus the instant it means."""
    target = datetime.now(IST).replace(hour=hour, minute=0, second=0, microsecond=0)
    target += timedelta(days=1)
    while target.weekday() == 6:
        target += timedelta(days=1)

    days_ahead = (target.date() - datetime.now(IST).date()).days
    day_phrase = "tomorrow" if days_ahead == 1 else target.strftime("%A")
    suffix = "am" if hour < 12 else "pm"
    spoken_hour = hour if hour <= 12 else hour - 12
    return f"{day_phrase} at {spoken_hour} {suffix}", target


@pytest.mark.asyncio
class TestBookAppointmentNode:
    async def _run(
        self,
        session: AsyncSession,
        business: Business,
        call: CallLog,
        utterances: list[str],
    ) -> tuple[VoiceAgent, list]:
        """Drive the booking flow through ``utterances`` and return the turns."""
        agent = booking_agent(business)
        session.add(agent)
        await session.flush()
        call.agent_id = agent.id
        await session.flush()

        engine = ConversationEngine()
        await engine.start_call(session, call, agent)
        results = []
        for utterance in utterances:
            results.append(await engine.process_turn(session, call, agent, utterance))
        return agent, results

    async def test_one_reply_answers_only_the_question_that_was_asked(
        self, session: AsyncSession, business: Business, call: CallLog
    ) -> None:
        """A walk that reaches a second ``collect`` must stop and ask it.

        Otherwise a single "Asha Menon" fills both the name and the preferred
        time, and the agent books whatever it can make of the caller's name.
        """
        _, results = await self._run(session, business, call, ["Asha Menon"])

        variables = call.metadata_json["state"]["variables"]
        assert variables["customer_name"] == "Asha Menon"
        assert "preferred_time" not in variables
        assert call.metadata_json["state"]["current_node"] == "ask_time"
        assert "which day and time" in results[-1].reply.lower()

    async def test_a_caller_books_a_slot_end_to_end(
        self, session: AsyncSession, business: Business, call: CallLog
    ) -> None:
        phrase, expected = next_open_phrase()

        agent, results = await self._run(session, business, call, ["Asha Menon", phrase])

        appointment = (
            await session.execute(select(Appointment).where(Appointment.call_id == call.id))
        ).scalar_one()
        assert appointment.customer_name == "Asha Menon"
        assert appointment.customer_phone == call.caller_number
        assert appointment.source == AppointmentSource.VOICE_CALL
        assert appointment.agent_id == agent.id
        assert to_zone(appointment.scheduled_at, "Asia/Kolkata") == expected
        assert "booked" in results[-1].reply.lower()

    async def test_the_booking_ends_the_call_as_resolved(
        self, session: AsyncSession, business: Business, call: CallLog
    ) -> None:
        phrase, _ = next_open_phrase()

        _, results = await self._run(session, business, call, ["Asha Menon", phrase])

        assert results[-1].should_end_call is True
        assert call.resolution == "resolved"

    async def test_an_unparseable_time_asks_again_instead_of_failing(
        self, session: AsyncSession, business: Business, call: CallLog, fake_llm: FakeLLMClient
    ) -> None:
        fake_llm.queue_json({"date": None, "time": None})

        _, results = await self._run(session, business, call, ["Asha Menon", "whenever"])

        reply = results[-1].reply.lower()
        assert "which day" in reply or "did not catch" in reply
        assert await session.scalar(select(Appointment.id)) is None

    async def test_a_taken_slot_offers_the_next_openings(
        self, session: AsyncSession, business: Business, call: CallLog
    ) -> None:
        phrase, wanted = next_open_phrase()
        from app.schemas.appointment import AppointmentCreate

        await appointment_service.book(
            session,
            business.id,
            AppointmentCreate(
                customer_name="Ravi Kumar",
                customer_phone="9800000001",
                scheduled_at=wanted,
            ),
        )

        _, results = await self._run(session, business, call, ["Asha Menon", phrase])

        reply = results[-1].reply.lower()
        assert "taken" in reply
        # The alternatives are also left on the call state for the retry node.
        alternatives = call.metadata_json["state"]["variables"]["appointment_alternatives"]
        assert len(alternatives) == 2

    async def test_a_time_outside_opening_hours_is_not_booked(
        self, session: AsyncSession, business: Business, call: CallLog
    ) -> None:
        _, results = await self._run(session, business, call, ["Asha Menon", "tomorrow at 11 pm"])

        assert await session.scalar(select(Appointment.id)) is None
        assert "taken" in results[-1].reply.lower() or "not available" in results[-1].reply.lower()

    async def test_the_retry_path_books_the_second_choice(
        self, session: AsyncSession, business: Business, call: CallLog
    ) -> None:
        first, wanted = next_open_phrase(11)
        second, _ = next_open_phrase(12)
        from app.schemas.appointment import AppointmentCreate

        await appointment_service.book(
            session,
            business.id,
            AppointmentCreate(
                customer_name="Ravi Kumar",
                customer_phone="9800000001",
                scheduled_at=wanted,
            ),
        )

        await self._run(session, business, call, ["Asha Menon", first, second])

        booked = (
            await session.execute(select(Appointment).where(Appointment.call_id == call.id))
        ).scalar_one()
        assert to_zone(booked.scheduled_at, "Asia/Kolkata").hour == 12
        assert booked.status == AppointmentStatus.SCHEDULED

    async def test_an_outbound_call_books_against_the_callee(
        self, session: AsyncSession, business: Business, call: CallLog
    ) -> None:
        call.direction = CallDirection.OUTBOUND
        call.caller_number = "+918000000001"
        call.callee_number = "+919999977777"
        await session.flush()
        phrase, _ = next_open_phrase()

        await self._run(session, business, call, ["Asha Menon", phrase])

        appointment = (
            await session.execute(select(Appointment).where(Appointment.call_id == call.id))
        ).scalar_one()
        assert appointment.customer_phone == "+919999977777"

    async def test_a_missing_name_falls_back_rather_than_blocking_the_booking(
        self, session: AsyncSession, business: Business, call: CallLog
    ) -> None:
        phrase, _ = next_open_phrase()

        await self._run(session, business, call, ["", phrase])

        appointment = (
            await session.execute(select(Appointment).where(Appointment.call_id == call.id))
        ).scalar_one()
        assert appointment.customer_name == "Phone caller"

    async def test_the_appointment_is_scoped_to_the_calling_tenant(
        self,
        session: AsyncSession,
        business: Business,
        other_business: Business,
        call: CallLog,
    ) -> None:
        phrase, _ = next_open_phrase()

        await self._run(session, business, call, ["Asha Menon", phrase])

        rows, total = await appointment_service.list_appointments(session, other_business.id)
        assert total == 0
        assert rows == []


class TestDefaultBookingFlow:
    def test_an_appointment_agent_gets_a_flow_that_actually_books(self) -> None:
        from app.services.flow import default_flow

        flow = validate_flow(default_flow("Namaste!", "appointment_booking"))

        assert flow is not None
        assert any(node.type == "book_appointment" for node in flow.nodes)

    def test_other_use_cases_keep_the_generic_starter_flow(self) -> None:
        from app.services.flow import default_flow

        flow = validate_flow(default_flow("Namaste!", "customer_support"))

        assert flow is not None
        assert not any(node.type == "book_appointment" for node in flow.nodes)

    def test_the_booking_flow_routes_every_failure_somewhere(self) -> None:
        flow = validate_flow(appointment_booking_flow("Namaste!"))

        assert flow is not None
        booking = flow.get_node("book")
        assert booking.on_unavailable == "retry_time"
        assert booking.on_error == "retry_time"
        # The retry gives up to a human rather than looping forever.
        assert flow.get_node("book_retry").on_unavailable == "handoff"
