"""Business opening hours and slot generation.

A tenant's calendar lives on ``Business.settings_json`` rather than in its own
table — it is small, read on every availability check, and edited as a whole::

    {
      "business_hours": {
        "mon": [["09:00", "13:00"], ["14:00", "18:00"]],
        "sun": []
      },
      "appointments": {
        "timezone": "Asia/Kolkata",
        "slot_minutes": 30,
        "capacity_per_slot": 1,
        "min_notice_minutes": 30,
        "max_advance_days": 60,
        "closed_dates": ["2026-08-15"]
      }
    }

Anything absent falls back to the defaults below, so a business that has never
opened the settings screen still has a working calendar.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.core.config import settings
from app.core.errors import ValidationError

#: Index 0 == Monday, matching ``date.weekday()``.
WEEKDAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

#: Indian SMBs typically open six days with a midday break; Sunday is closed.
DEFAULT_WINDOWS: tuple[tuple[time, time], ...] = (
    (time(9, 0), time(13, 0)),
    (time(14, 0), time(18, 0)),
)
DEFAULT_HOURS: dict[str, tuple[tuple[time, time], ...]] = {
    **{key: DEFAULT_WINDOWS for key in WEEKDAY_KEYS[:6]},
    "sun": (),
}

DEFAULT_SLOT_MINUTES = 30
DEFAULT_CAPACITY = 1
DEFAULT_MIN_NOTICE_MINUTES = 30
DEFAULT_MAX_ADVANCE_DAYS = 60

MAX_DURATION_MINUTES = 480


@dataclass(frozen=True, slots=True)
class ScheduleConfig:
    """A tenant's resolved calendar rules."""

    timezone: str
    slot_minutes: int
    capacity_per_slot: int
    min_notice_minutes: int
    max_advance_days: int
    hours: dict[str, tuple[tuple[time, time], ...]]
    closed_dates: frozenset[date]

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def now(self) -> datetime:
        return datetime.now(self.zone)

    def is_open_on(self, day: date) -> bool:
        return day not in self.closed_dates and bool(self.windows_for(day))

    def windows_for(self, day: date) -> tuple[tuple[time, time], ...]:
        if day in self.closed_dates:
            return ()
        return self.hours.get(WEEKDAY_KEYS[day.weekday()], ())

    def localize(self, day: date, moment: time) -> datetime:
        return datetime.combine(day, moment, tzinfo=self.zone)


def load_schedule_config(settings_json: dict | None) -> ScheduleConfig:
    """Resolve a tenant's calendar rules, falling back to platform defaults.

    Malformed entries are ignored rather than raised: a bad value saved months
    ago must not take the booking flow down mid-call.
    """
    raw = settings_json or {}
    candidate = raw.get("appointments")
    options: dict = candidate if isinstance(candidate, dict) else {}

    return ScheduleConfig(
        timezone=_timezone(options.get("timezone")),
        slot_minutes=_bounded(options.get("slot_minutes"), DEFAULT_SLOT_MINUTES, 5, 240),
        capacity_per_slot=_bounded(options.get("capacity_per_slot"), DEFAULT_CAPACITY, 1, 100),
        min_notice_minutes=_bounded(
            options.get("min_notice_minutes"), DEFAULT_MIN_NOTICE_MINUTES, 0, 10_080
        ),
        max_advance_days=_bounded(
            options.get("max_advance_days"), DEFAULT_MAX_ADVANCE_DAYS, 1, 730
        ),
        hours=_parse_hours(raw.get("business_hours")),
        closed_dates=_parse_closed_dates(options.get("closed_dates")),
    )


def _timezone(value: object) -> str:
    if isinstance(value, str) and value:
        try:
            ZoneInfo(value)
        except Exception:
            return settings.trai_timezone
        return value
    return settings.trai_timezone


def _bounded(value: object, default: int, low: int, high: int) -> int:
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def _parse_hours(value: object) -> dict[str, tuple[tuple[time, time], ...]]:
    if not isinstance(value, dict):
        return dict(DEFAULT_HOURS)

    hours: dict[str, tuple[tuple[time, time], ...]] = {}
    for key in WEEKDAY_KEYS:
        if key not in value:
            hours[key] = DEFAULT_HOURS[key]
            continue
        hours[key] = _parse_windows(value.get(key))
    return hours


def _parse_windows(value: object) -> tuple[tuple[time, time], ...]:
    """Parse ``[["09:00", "13:00"], ...]`` into ordered, valid time windows."""
    if not isinstance(value, list):
        return ()
    windows: list[tuple[time, time]] = []
    for entry in value:
        if not isinstance(entry, (list, tuple)) or len(entry) != 2:
            continue
        start, end = _parse_time(entry[0]), _parse_time(entry[1])
        if start is None or end is None or start >= end:
            continue
        windows.append((start, end))
    return tuple(sorted(windows))


def _parse_time(value: object) -> time | None:
    if isinstance(value, time):
        return value
    if not isinstance(value, str):
        return None
    try:
        parsed = time.fromisoformat(value.strip())
    except ValueError:
        return None
    return parsed.replace(second=0, microsecond=0, tzinfo=None)


def _parse_closed_dates(value: object) -> frozenset[date]:
    if not isinstance(value, list):
        return frozenset()
    days: set[date] = set()
    for entry in value:
        if isinstance(entry, date):
            days.add(entry)
        elif isinstance(entry, str):
            try:
                days.add(date.fromisoformat(entry.strip()))
            except ValueError:
                continue
    return frozenset(days)


def candidate_slots(config: ScheduleConfig, day: date, duration_minutes: int) -> list[datetime]:
    """Every slot start on ``day`` that fits ``duration_minutes`` inside a window.

    Slots step by ``slot_minutes``, so a 60-minute appointment on a 30-minute
    grid can start at 09:00, 09:30, 10:00 … as long as it ends before the
    window closes. Availability (who is already booked) is applied separately.
    """
    if duration_minutes <= 0:
        raise ValidationError("Appointment duration must be positive.")

    step = timedelta(minutes=config.slot_minutes)
    length = timedelta(minutes=duration_minutes)
    slots: list[datetime] = []

    for window_start, window_end in config.windows_for(day):
        cursor = config.localize(day, window_start)
        closes = config.localize(day, window_end)
        while cursor + length <= closes:
            slots.append(cursor)
            cursor += step
    return slots


def within_business_hours(config: ScheduleConfig, start: datetime, duration_minutes: int) -> bool:
    """True when ``[start, start+duration)`` sits entirely inside one open window."""
    local = start.astimezone(config.zone)
    end = local + timedelta(minutes=duration_minutes)
    for window_start, window_end in config.windows_for(local.date()):
        opens = config.localize(local.date(), window_start)
        closes = config.localize(local.date(), window_end)
        if opens <= local and end <= closes:
            return True
    return False


def booking_horizon(config: ScheduleConfig, now: datetime | None = None) -> tuple[datetime, date]:
    """The earliest bookable instant and the last bookable date."""
    moment = (now or config.now()).astimezone(config.zone)
    earliest = moment + timedelta(minutes=config.min_notice_minutes)
    latest = moment.date() + timedelta(days=config.max_advance_days)
    return earliest, latest
