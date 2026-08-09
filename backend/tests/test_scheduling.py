"""Opening hours parsing and slot generation."""

from __future__ import annotations

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import pytest

from app.core.errors import ValidationError
from app.services import scheduling

IST = ZoneInfo("Asia/Kolkata")

#: 2026-08-10 is a Monday, 2026-08-16 a Sunday.
MONDAY = date(2026, 8, 10)
SUNDAY = date(2026, 8, 16)


class TestLoadScheduleConfig:
    def test_defaults_apply_to_a_business_that_never_configured_hours(self) -> None:
        config = scheduling.load_schedule_config(None)

        assert config.timezone == "Asia/Kolkata"
        assert config.slot_minutes == scheduling.DEFAULT_SLOT_MINUTES
        assert config.capacity_per_slot == 1
        assert config.is_open_on(MONDAY)
        assert not config.is_open_on(SUNDAY)

    def test_configured_hours_replace_the_defaults_for_named_days(self) -> None:
        config = scheduling.load_schedule_config(
            {"business_hours": {"mon": [["10:00", "12:00"]], "sun": [["11:00", "13:00"]]}}
        )

        assert config.windows_for(MONDAY) == ((time(10, 0), time(12, 0)),)
        assert config.windows_for(SUNDAY) == ((time(11, 0), time(13, 0)),)
        # Untouched days keep the platform default.
        assert config.windows_for(date(2026, 8, 11)) == scheduling.DEFAULT_WINDOWS

    def test_windows_are_sorted_so_slot_generation_runs_in_clock_order(self) -> None:
        config = scheduling.load_schedule_config(
            {"business_hours": {"mon": [["14:00", "18:00"], ["09:00", "13:00"]]}}
        )

        assert config.windows_for(MONDAY) == (
            (time(9, 0), time(13, 0)),
            (time(14, 0), time(18, 0)),
        )

    @pytest.mark.parametrize(
        "windows",
        [
            [["18:00", "09:00"]],  # ends before it starts
            [["not-a-time", "18:00"]],
            [["09:00"]],  # wrong arity
            "09:00-18:00",  # not a list
        ],
    )
    def test_malformed_windows_are_dropped_not_raised(self, windows: object) -> None:
        """A bad value saved months ago must not break a live call."""
        config = scheduling.load_schedule_config({"business_hours": {"mon": windows}})

        assert config.windows_for(MONDAY) == ()

    def test_out_of_range_numbers_are_clamped(self) -> None:
        config = scheduling.load_schedule_config(
            {"appointments": {"slot_minutes": 9999, "capacity_per_slot": 0}}
        )

        assert config.slot_minutes == 240
        assert config.capacity_per_slot == 1

    def test_unknown_timezone_falls_back_to_the_platform_default(self) -> None:
        config = scheduling.load_schedule_config({"appointments": {"timezone": "Mars/Olympus"}})

        assert config.timezone == "Asia/Kolkata"

    def test_closed_dates_shut_an_otherwise_open_day(self) -> None:
        config = scheduling.load_schedule_config(
            {"appointments": {"closed_dates": ["2026-08-10", "garbage"]}}
        )

        assert not config.is_open_on(MONDAY)
        assert config.windows_for(MONDAY) == ()
        assert config.is_open_on(date(2026, 8, 11))


class TestCandidateSlots:
    def test_slots_step_by_slot_minutes_within_each_window(self) -> None:
        config = scheduling.load_schedule_config(
            {"business_hours": {"mon": [["09:00", "10:30"]]}, "appointments": {"slot_minutes": 30}}
        )

        slots = scheduling.candidate_slots(config, MONDAY, 30)

        assert [s.strftime("%H:%M") for s in slots] == ["09:00", "09:30", "10:00"]
        assert slots[0].tzinfo is not None

    def test_a_long_appointment_may_not_overhang_the_window(self) -> None:
        config = scheduling.load_schedule_config(
            {"business_hours": {"mon": [["09:00", "10:30"]]}, "appointments": {"slot_minutes": 30}}
        )

        slots = scheduling.candidate_slots(config, MONDAY, 60)

        assert [s.strftime("%H:%M") for s in slots] == ["09:00", "09:30"]

    def test_both_windows_of_a_split_day_produce_slots(self) -> None:
        config = scheduling.load_schedule_config(None)

        slots = scheduling.candidate_slots(config, MONDAY, 30)
        hours = {s.hour for s in slots}

        assert 9 in hours and 14 in hours
        assert 13 not in hours  # the lunch break is not bookable

    def test_a_closed_day_has_no_slots(self) -> None:
        config = scheduling.load_schedule_config(None)

        assert scheduling.candidate_slots(config, SUNDAY, 30) == []

    def test_non_positive_duration_is_rejected(self) -> None:
        config = scheduling.load_schedule_config(None)

        with pytest.raises(ValidationError):
            scheduling.candidate_slots(config, MONDAY, 0)


class TestWithinBusinessHours:
    def test_an_appointment_inside_a_window_is_accepted(self) -> None:
        config = scheduling.load_schedule_config(None)
        start = datetime(2026, 8, 10, 9, 30, tzinfo=IST)

        assert scheduling.within_business_hours(config, start, 30)

    def test_an_appointment_spilling_past_closing_is_rejected(self) -> None:
        config = scheduling.load_schedule_config(None)
        start = datetime(2026, 8, 10, 12, 45, tzinfo=IST)

        assert not scheduling.within_business_hours(config, start, 30)

    def test_an_appointment_straddling_the_lunch_break_is_rejected(self) -> None:
        config = scheduling.load_schedule_config(None)
        start = datetime(2026, 8, 10, 12, 30, tzinfo=IST)

        assert not scheduling.within_business_hours(config, start, 120)

    def test_the_check_converts_from_other_timezones(self) -> None:
        """10:00 IST arrives as 04:30 UTC; it is still inside opening hours."""
        config = scheduling.load_schedule_config(None)
        start = datetime(2026, 8, 10, 4, 30, tzinfo=ZoneInfo("UTC"))

        assert scheduling.within_business_hours(config, start, 30)


class TestBookingHorizon:
    def test_notice_and_advance_limits_bracket_the_bookable_range(self) -> None:
        config = scheduling.load_schedule_config(
            {"appointments": {"min_notice_minutes": 60, "max_advance_days": 7}}
        )
        now = datetime(2026, 8, 10, 9, 0, tzinfo=IST)

        earliest, latest = scheduling.booking_horizon(config, now)

        assert earliest == datetime(2026, 8, 10, 10, 0, tzinfo=IST)
        assert latest == date(2026, 8, 17)
