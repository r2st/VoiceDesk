"""WhatsApp handoff (design doc §4.5).

When a voice conversation cannot be resolved — the AI's confidence drops below
the agent's threshold, the caller asks for text, or a document is needed — the
conversation context is transferred to a WhatsApp channel and the caller gets a
summary message.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import ExternalServiceError, NotFoundError
from app.core.logging import get_logger, mask_phone
from app.models.call import CallLog, Conversation, WhatsAppHandoff
from app.models.enums import CallResolution, HandoffReason, HandoffStatus, Language, SpeakerRole
from app.models.voice_agent import VoiceAgent

logger = get_logger(__name__)

#: Used when an agent has no threshold of its own. Matches the column default.
DEFAULT_HANDOFF_THRESHOLD = 0.70

HANDOFF_TEMPLATES = {
    "hi": (
        "नमस्ते! अभी हमारी फ़ोन पर बात हुई थी। यहाँ आपकी बातचीत का सारांश है:\n\n"
        "{summary}\n\nआप यहीं WhatsApp पर जवाब दे सकते हैं।"
    ),
    "en": (
        "Hello! We were just speaking on the phone. Here is a summary of your "
        "conversation:\n\n{summary}\n\nYou can reply right here on WhatsApp."
    ),
}


@dataclass(slots=True)
class WhatsAppMessage:
    to_number: str
    body: str
    context: dict | None = None


@dataclass(slots=True)
class WhatsAppResult:
    message_id: str
    accepted: bool
    raw: dict | None = None
    error: str | None = None


class WhatsAppProvider(ABC):
    name = "base"

    @abstractmethod
    async def send(self, message: WhatsAppMessage) -> WhatsAppResult: ...


class MockWhatsAppProvider(WhatsAppProvider):
    """Records messages in memory instead of sending them."""

    name = "mock"

    def __init__(self, fail: bool = False) -> None:
        self.sent: list[WhatsAppMessage] = []
        self.fail = fail

    async def send(self, message: WhatsAppMessage) -> WhatsAppResult:
        self.sent.append(message)
        if self.fail:
            return WhatsAppResult(message_id="", accepted=False, error="Simulated failure")
        return WhatsAppResult(
            message_id=f"mock-wa-{len(self.sent):06d}", accepted=True, raw={"provider": "mock"}
        )


class GoSumoProvider(WhatsAppProvider):
    """GoSumo WhatsApp Business channel (design doc §4.5, §4.6)."""

    name = "gosumo"

    def __init__(
        self,
        api_url: str | None = None,
        api_key: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.api_url = (api_url or settings.gosumo_api_url).rstrip("/")
        self.api_key = api_key or settings.gosumo_api_key
        self._client = client

    async def send(self, message: WhatsAppMessage) -> WhatsAppResult:
        if not self.api_key:
            raise ExternalServiceError("GOSUMO_API_KEY is not configured.")

        client = self._client or httpx.AsyncClient(timeout=15.0)
        owns_client = self._client is None
        try:
            response = await client.post(
                f"{self.api_url}/messages",
                json={
                    "to": message.to_number,
                    "type": "text",
                    "text": {"body": message.body},
                    "context": message.context or {},
                },
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
            )
            if response.status_code >= 400:
                return WhatsAppResult(
                    message_id="",
                    accepted=False,
                    error=f"HTTP {response.status_code}: {response.text[:200]}",
                )
            body = response.json()
            return WhatsAppResult(
                message_id=str(body.get("message_id") or body.get("id") or ""),
                accepted=True,
                raw=body,
            )
        except httpx.HTTPError as exc:
            return WhatsAppResult(message_id="", accepted=False, error=str(exc))
        finally:
            if owns_client:
                await client.aclose()


_provider: WhatsAppProvider | None = None


def get_whatsapp_provider() -> WhatsAppProvider:
    global _provider
    if _provider is None:
        _provider = (
            GoSumoProvider() if settings.whatsapp_provider == "gosumo" else MockWhatsAppProvider()
        )
    return _provider


def set_whatsapp_provider(provider: WhatsAppProvider | None) -> None:
    global _provider
    _provider = provider


# --------------------------------------------------------------------------- #
# Handoff orchestration
# --------------------------------------------------------------------------- #
async def initiate_handoff(
    session: AsyncSession,
    call: CallLog,
    *,
    reason: HandoffReason,
    summary: str | None = None,
    confidence: float | None = None,
    language: Language | str = Language.HINDI,
    to_number: str | None = None,
) -> WhatsAppHandoff:
    """Create and send a WhatsApp handoff for a call. Idempotent per call.

    A failed send is still recorded with ``status=failed`` so it can be retried
    and so the dashboard shows the attempt.
    """
    existing = (
        await session.execute(
            select(WhatsAppHandoff).where(
                WhatsAppHandoff.call_id == call.id,
                WhatsAppHandoff.business_id == call.business_id,
                WhatsAppHandoff.deleted_at.is_(None),
                WhatsAppHandoff.status.in_([HandoffStatus.SENT, HandoffStatus.ACKNOWLEDGED]),
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    transcript = await _transcript(session, call)
    text = summary or call.summary or _fallback_summary(transcript)
    lang_code = language.value if isinstance(language, Language) else str(language)
    template = HANDOFF_TEMPLATES.get(lang_code, HANDOFF_TEMPLATES["en"])

    provider = get_whatsapp_provider()
    handoff = WhatsAppHandoff(
        business_id=call.business_id,
        call_id=call.id,
        to_number=to_number or call.caller_number,
        reason=reason,
        status=HandoffStatus.PENDING,
        summary=text,
        context_json={
            "call_id": str(call.id),
            "agent_id": str(call.agent_id) if call.agent_id else None,
            "language": lang_code,
            "primary_intent": call.primary_intent,
            "transcript": transcript,
        },
        provider=provider.name,
        confidence_at_handoff=confidence,
    )
    session.add(handoff)
    await session.flush()

    result = await provider.send(
        WhatsAppMessage(
            to_number=handoff.to_number,
            body=template.format(summary=text),
            context={"call_id": str(call.id), "reason": reason.value},
        )
    )

    if result.accepted:
        handoff.status = HandoffStatus.SENT
        handoff.provider_message_id = result.message_id
        handoff.sent_at = datetime.now(UTC)
        call.resolution = CallResolution.HANDED_OFF
        logger.info(
            "WhatsApp handoff sent for call %s to %s (%s)",
            call.id,
            mask_phone(handoff.to_number),
            reason.value,
        )
    else:
        handoff.status = HandoffStatus.FAILED
        handoff.error_message = (result.error or "Unknown error")[:500]
        logger.warning("WhatsApp handoff failed for call %s: %s", call.id, handoff.error_message)

    await session.flush()
    return handoff


async def retry_handoff(
    session: AsyncSession, business_id: uuid.UUID, handoff_id: uuid.UUID
) -> WhatsAppHandoff:
    handoff = (
        await session.execute(
            select(WhatsAppHandoff).where(
                WhatsAppHandoff.id == handoff_id,
                WhatsAppHandoff.business_id == business_id,
                WhatsAppHandoff.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if handoff is None:
        raise NotFoundError("Handoff not found.")
    if handoff.status in (HandoffStatus.SENT, HandoffStatus.ACKNOWLEDGED):
        return handoff

    language = (handoff.context_json or {}).get("language", "en")
    template = HANDOFF_TEMPLATES.get(language, HANDOFF_TEMPLATES["en"])
    provider = get_whatsapp_provider()
    result = await provider.send(
        WhatsAppMessage(
            to_number=handoff.to_number,
            body=template.format(summary=handoff.summary),
            context={"call_id": str(handoff.call_id), "reason": handoff.reason},
        )
    )
    if result.accepted:
        handoff.status = HandoffStatus.SENT
        handoff.provider_message_id = result.message_id
        handoff.sent_at = datetime.now(UTC)
        handoff.error_message = None
    else:
        handoff.status = HandoffStatus.FAILED
        handoff.error_message = (result.error or "Unknown error")[:500]
    await session.flush()
    return handoff


def should_handoff(
    *, confidence: float, agent: VoiceAgent, utterance: str = ""
) -> HandoffReason | None:
    """Decide whether a turn warrants escalating to WhatsApp."""
    if not agent.whatsapp_handoff_enabled:
        return None

    lowered = (utterance or "").lower()
    if any(
        phrase in lowered
        for phrase in ("whatsapp", "message me", "text me", "send me a message", "व्हाट्सएप")
    ):
        return HandoffReason.CALLER_REQUEST
    if any(
        phrase in lowered
        for phrase in ("send document", "share document", "upload", "photo", "receipt", "invoice")
    ):
        return HandoffReason.DOCUMENT_REQUIRED
    # `is None`, not `or`: the API accepts a threshold of 0.0, which means
    # "never escalate on confidence alone". `or` would read that as unset and
    # substitute 0.70, escalating on exactly the calls the tenant excluded.
    threshold = agent.handoff_confidence_threshold
    if confidence < (DEFAULT_HANDOFF_THRESHOLD if threshold is None else threshold):
        return HandoffReason.LOW_CONFIDENCE
    return None


async def _transcript(session: AsyncSession, call: CallLog) -> list[dict]:
    result = await session.execute(
        select(Conversation)
        .where(
            Conversation.call_id == call.id,
            Conversation.business_id == call.business_id,
            Conversation.deleted_at.is_(None),
        )
        .order_by(Conversation.turn_index)
    )
    return [
        {"role": turn.role, "content": turn.content, "language": turn.language}
        for turn in result.scalars().all()
    ]


def _fallback_summary(transcript: list[dict]) -> str:
    """A plain summary when the LLM has not produced one."""
    caller_lines = [t["content"] for t in transcript if t["role"] == SpeakerRole.CALLER]
    if not caller_lines:
        return "We were unable to complete your request over the phone."
    return "You asked about: " + "; ".join(line[:120] for line in caller_lines[:3])
