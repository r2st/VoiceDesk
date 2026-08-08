"""TRAI regulatory compliance (design doc §8.2).

Five rules govern every outbound call:

1. DND registry check before dialling.
2. Calling hours restricted to 09:00–21:00 IST.
3. Caller ID must be displayed.
4. A recording-consent announcement opens the call.
5. An opt-out is offered during every call.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import get_logger, mask_phone
from app.models.call import DNDRegistry

logger = get_logger(__name__)

CONSENT_ANNOUNCEMENTS = {
    "hi": (
        "नमस्ते। सेवा की गुणवत्ता के लिए इस कॉल की रिकॉर्डिंग की जा रही है। "
        "इस सूची से हटने के लिए कभी भी 'बंद करें' कहें।"
    ),
    "en": (
        "Hello. This call is being recorded for quality and training purposes. "
        "Say 'stop calling' at any time to opt out of future calls."
    ),
    "ta": "வணக்கம். இந்த அழைப்பு தரக் கண்காணிப்புக்காக பதிவு செய்யப்படுகிறது.",
    "te": "నమస్కారం. నాణ్యత కోసం ఈ కాల్ రికార్డ్ చేయబడుతోంది.",
    "mr": "नमस्कार. सेवा गुणवत्तेसाठी हा कॉल रेकॉर्ड केला जात आहे.",
    "bn": "নমস্কার। মান নিয়ন্ত্রণের জন্য এই কলটি রেকর্ড করা হচ্ছে।",
    "kn": "ನಮಸ್ಕಾರ. ಗುಣಮಟ್ಟಕ್ಕಾಗಿ ಈ ಕರೆಯನ್ನು ರೆಕಾರ್ಡ್ ಮಾಡಲಾಗುತ್ತಿದೆ.",
}

#: Phrases that count as an explicit opt-out request from the caller.
OPT_OUT_PHRASES = (
    "stop calling",
    "do not call",
    "don't call",
    "remove my number",
    "unsubscribe",
    "opt out",
    "मुझे कॉल मत करो",
    "कॉल बंद करो",
    "बंद करें",
    "मेरा नंबर हटाओ",
)


@dataclass(frozen=True, slots=True)
class ComplianceDecision:
    allowed: bool
    reason: str | None = None
    code: str | None = None
    details: dict | None = None


def now_ist() -> datetime:
    return datetime.now(ZoneInfo(settings.trai_timezone))


def within_calling_hours(when: datetime | None = None) -> bool:
    """True when ``when`` (IST) falls inside the TRAI-permitted window."""
    moment = when or now_ist()
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    local = moment.astimezone(ZoneInfo(settings.trai_timezone))
    return settings.trai_calling_hour_start <= local.hour < settings.trai_calling_hour_end


def next_allowed_slot(when: datetime | None = None) -> datetime:
    """The next moment an outbound call would be permitted, in IST."""
    tz = ZoneInfo(settings.trai_timezone)
    local = (when or now_ist()).astimezone(tz)
    start = settings.trai_calling_hour_start

    if local.hour < start:
        return local.replace(hour=start, minute=0, second=0, microsecond=0)
    if local.hour >= settings.trai_calling_hour_end:
        tomorrow = (local + timedelta(days=1)).replace(
            hour=start, minute=0, second=0, microsecond=0
        )
        return tomorrow
    return local


async def is_dnd(
    session: AsyncSession, phone_number: str, business_id: uuid.UUID | None = None
) -> bool:
    """True if the number is on the national DND list or the tenant's opt-out list."""
    if not settings.trai_dnd_check_enabled:
        return False

    # A NULL business_id row is a national-registry entry and applies to everyone.
    scope = DNDRegistry.business_id.is_(None)
    if business_id is not None:
        scope = or_(scope, DNDRegistry.business_id == business_id)

    stmt = select(DNDRegistry).where(
        DNDRegistry.phone_number == phone_number,
        DNDRegistry.is_dnd.is_(True),
        scope,
    )
    result = await session.execute(stmt.limit(1))
    return result.scalar_one_or_none() is not None


async def check_outbound_call(
    session: AsyncSession,
    *,
    to_number: str,
    business_id: uuid.UUID,
    caller_id: str | None,
    scheduled_at: datetime | None = None,
) -> ComplianceDecision:
    """Run every pre-dial TRAI rule. Returns the first violation, if any."""
    if not caller_id:
        return ComplianceDecision(
            allowed=False,
            reason="A caller ID must be displayed on outbound calls (TRAI).",
            code="caller_id_required",
        )

    if await is_dnd(session, to_number, business_id):
        logger.info("Blocked outbound call to %s: DND registry", mask_phone(to_number))
        return ComplianceDecision(
            allowed=False,
            reason="This number is registered on the DND list.",
            code="dnd_blocked",
        )

    moment = scheduled_at or now_ist()
    if not within_calling_hours(moment):
        allowed_from = next_allowed_slot(moment)
        return ComplianceDecision(
            allowed=False,
            reason=(
                f"Outbound calls are restricted to "
                f"{settings.trai_calling_hour_start:02d}:00–"
                f"{settings.trai_calling_hour_end:02d}:00 IST."
            ),
            code="outside_calling_hours",
            details={"next_allowed_at": allowed_from.isoformat()},
        )

    return ComplianceDecision(allowed=True)


def consent_announcement(language: str = "hi") -> str | None:
    """The recording-consent line played at the start of a call."""
    if not settings.trai_recording_consent_enabled:
        return None
    return CONSENT_ANNOUNCEMENTS.get(language, CONSENT_ANNOUNCEMENTS["en"])


def detect_opt_out(utterance: str) -> bool:
    lowered = (utterance or "").lower()
    return any(phrase in lowered for phrase in OPT_OUT_PHRASES)


async def record_opt_out(
    session: AsyncSession, phone_number: str, business_id: uuid.UUID, source: str = "in_call"
) -> DNDRegistry:
    """Add a caller to this tenant's opt-out list (idempotent)."""
    existing = (
        await session.execute(
            select(DNDRegistry).where(
                DNDRegistry.phone_number == phone_number,
                DNDRegistry.business_id == business_id,
            )
        )
    ).scalar_one_or_none()

    if existing is not None:
        existing.is_dnd = True
        existing.source = source
        existing.checked_at = datetime.now(UTC)
        await session.flush()
        return existing

    entry = DNDRegistry(
        business_id=business_id,
        phone_number=phone_number,
        is_dnd=True,
        source=source,
        checked_at=datetime.now(UTC),
    )
    session.add(entry)
    await session.flush()
    logger.info("Recorded opt-out for %s", mask_phone(phone_number))
    return entry
