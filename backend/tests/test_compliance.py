"""TRAI regulatory compliance (design doc §8.2)."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.core.config import settings
from app.models.call import DNDRegistry
from app.services import compliance

IST = ZoneInfo("Asia/Kolkata")


def ist(year=2026, month=8, day=10, hour=12, minute=0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=IST)


class TestCallingHours:
    @pytest.mark.parametrize("hour", [9, 10, 14, 20])
    def test_inside_window(self, hour):
        assert compliance.within_calling_hours(ist(hour=hour))

    @pytest.mark.parametrize("hour", [0, 5, 8, 21, 22, 23])
    def test_outside_window(self, hour):
        assert not compliance.within_calling_hours(ist(hour=hour))

    def test_boundaries_are_inclusive_at_start_exclusive_at_end(self):
        # 09:00 is allowed; 21:00 is not — the window is [09:00, 21:00).
        assert compliance.within_calling_hours(ist(hour=9, minute=0))
        assert compliance.within_calling_hours(ist(hour=20, minute=59))
        assert not compliance.within_calling_hours(ist(hour=21, minute=0))

    def test_naive_datetime_is_treated_as_ist(self):
        assert compliance.within_calling_hours(datetime(2026, 8, 10, 12, 0))

    def test_utc_datetime_is_converted_before_comparison(self):
        from datetime import UTC

        # 05:00 UTC is 10:30 IST — inside the window even though 5 is not.
        assert compliance.within_calling_hours(datetime(2026, 8, 10, 5, 0, tzinfo=UTC))
        # 17:00 UTC is 22:30 IST — outside, even though 17 is inside.
        assert not compliance.within_calling_hours(datetime(2026, 8, 10, 17, 0, tzinfo=UTC))


class TestNextAllowedSlot:
    def test_before_the_window_moves_to_this_morning(self):
        slot = compliance.next_allowed_slot(ist(hour=6))
        assert (slot.hour, slot.day) == (settings.trai_calling_hour_start, 10)

    def test_after_the_window_moves_to_tomorrow_morning(self):
        slot = compliance.next_allowed_slot(ist(hour=22))
        assert (slot.hour, slot.day) == (settings.trai_calling_hour_start, 11)

    def test_inside_the_window_is_unchanged(self):
        moment = ist(hour=12)
        assert compliance.next_allowed_slot(moment) == moment


class TestDNDRegistry:
    async def test_unlisted_number_is_not_dnd(self, session, business):
        assert not await compliance.is_dnd(session, "+919999900000", business.id)

    async def test_national_registry_entry_applies_to_every_tenant(
        self, session, business, other_business
    ):
        session.add(DNDRegistry(business_id=None, phone_number="+919999900000", is_dnd=True))
        await session.flush()

        assert await compliance.is_dnd(session, "+919999900000", business.id)
        assert await compliance.is_dnd(session, "+919999900000", other_business.id)

    async def test_tenant_opt_out_does_not_leak_to_other_tenants(
        self, session, business, other_business
    ):
        await compliance.record_opt_out(session, "+919999900000", business.id)

        assert await compliance.is_dnd(session, "+919999900000", business.id)
        assert not await compliance.is_dnd(session, "+919999900000", other_business.id)

    async def test_record_opt_out_is_idempotent(self, session, business):
        first = await compliance.record_opt_out(session, "+919999900000", business.id)
        second = await compliance.record_opt_out(
            session, "+919999900000", business.id, source="dashboard"
        )
        assert first.id == second.id
        assert second.source == "dashboard"

    async def test_check_can_be_disabled_by_configuration(self, session, business, monkeypatch):
        session.add(DNDRegistry(business_id=None, phone_number="+919999900000", is_dnd=True))
        await session.flush()

        monkeypatch.setattr(settings, "trai_dnd_check_enabled", False)
        assert not await compliance.is_dnd(session, "+919999900000", business.id)


class TestOutboundCheck:
    async def test_allows_a_clean_call_in_hours(self, session, business):
        decision = await compliance.check_outbound_call(
            session,
            to_number="+919999900000",
            business_id=business.id,
            caller_id="+918000000001",
            scheduled_at=ist(hour=11),
        )
        assert decision.allowed

    async def test_requires_a_caller_id(self, session, business):
        decision = await compliance.check_outbound_call(
            session,
            to_number="+919999900000",
            business_id=business.id,
            caller_id=None,
            scheduled_at=ist(hour=11),
        )
        assert not decision.allowed
        assert decision.code == "caller_id_required"

    async def test_blocks_dnd_numbers(self, session, business):
        await compliance.record_opt_out(session, "+919999900000", business.id)
        decision = await compliance.check_outbound_call(
            session,
            to_number="+919999900000",
            business_id=business.id,
            caller_id="+918000000001",
            scheduled_at=ist(hour=11),
        )
        assert not decision.allowed
        assert decision.code == "dnd_blocked"

    async def test_blocks_outside_calling_hours_and_suggests_the_next_slot(self, session, business):
        decision = await compliance.check_outbound_call(
            session,
            to_number="+919999900000",
            business_id=business.id,
            caller_id="+918000000001",
            scheduled_at=ist(hour=23),
        )
        assert not decision.allowed
        assert decision.code == "outside_calling_hours"
        assert "next_allowed_at" in (decision.details or {})

    async def test_dnd_is_checked_before_calling_hours(self, session, business):
        """A DND number must be reported as DND even at a forbidden hour."""
        await compliance.record_opt_out(session, "+919999900000", business.id)
        decision = await compliance.check_outbound_call(
            session,
            to_number="+919999900000",
            business_id=business.id,
            caller_id="+918000000001",
            scheduled_at=ist(hour=23),
        )
        assert decision.code == "dnd_blocked"


class TestConsentAndOptOut:
    @pytest.mark.parametrize("language", ["hi", "en", "ta", "te", "mr", "bn", "kn"])
    def test_every_supported_language_has_an_announcement(self, language):
        announcement = compliance.consent_announcement(language)
        assert announcement and len(announcement) > 10

    def test_unknown_language_falls_back_to_english(self):
        assert compliance.consent_announcement("fr") == compliance.consent_announcement("en")

    def test_announcement_can_be_disabled(self, monkeypatch):
        monkeypatch.setattr(settings, "trai_recording_consent_enabled", False)
        assert compliance.consent_announcement("hi") is None

    @pytest.mark.parametrize(
        "utterance",
        [
            "please stop calling me",
            "DO NOT CALL again",
            "remove my number from your list",
            "I want to opt out",
            "मुझे कॉल मत करो",
            "कॉल बंद करो",
        ],
    )
    def test_opt_out_phrases_are_detected(self, utterance):
        assert compliance.detect_opt_out(utterance)

    @pytest.mark.parametrize(
        "utterance",
        ["I want to book an appointment", "call me back tomorrow", "", "haan theek hai"],
    )
    def test_ordinary_speech_is_not_an_opt_out(self, utterance):
        assert not compliance.detect_opt_out(utterance)
