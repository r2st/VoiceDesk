"""Timezone helpers.

Everything is stored in UTC and shown to staff in the business timezone
(``Asia/Kolkata`` unless the tenant overrides it). SQLite — which the test
suite uses — drops the offset on the way out of ``DateTime(timezone=True)``,
so anything read back from the database has to be re-stamped before it can be
compared with an aware "now".
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo


def ensure_utc(value: datetime) -> datetime:
    """Return ``value`` as an aware UTC datetime, treating naive input as UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def to_zone(value: datetime, timezone: str) -> datetime:
    """Present a stored instant in a display timezone."""
    return ensure_utc(value).astimezone(ZoneInfo(timezone))


def now_in(timezone: str) -> datetime:
    return datetime.now(ZoneInfo(timezone))
