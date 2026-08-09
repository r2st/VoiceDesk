"""``/api/v1/webhooks`` — inbound telephony and call-status events.

These endpoints are called by the telephony provider, not the dashboard, so
they carry no JWT. Authenticity comes from an HMAC-SHA256 signature over the
raw request body (design doc §5.1, §8.3).
"""

from __future__ import annotations

import json
from typing import Annotated, Any

from fastapi import APIRouter, Header, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.deps import DbSession
from app.core.errors import AuthenticationError, ValidationError
from app.core.logging import get_logger, mask_phone
from app.core.security import verify_webhook_signature
from app.models.call import CallLog
from app.models.enums import CallStatus
from app.schemas.call import WebhookAck
from app.services import call_service, recording_service
from app.services.telephony import WebhookEvent, get_provider

logger = get_logger(__name__)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])

#: Header names the supported providers use for their body signature.
SIGNATURE_HEADERS = ("x-voicedesk-signature", "x-exotel-signature", "x-knowlarity-signature")


async def _verified_body(request: Request, signature: str | None) -> tuple[bytes, dict[str, Any]]:
    """Read the raw body, check its HMAC, and decode it as JSON or form data.

    The signature is computed over the *raw* bytes, so it must be read before
    any parsing. Verification is skipped only outside production and only when
    no signing secret has been configured.
    """
    body = await request.body()

    header_signature = signature
    if header_signature is None:
        for name in SIGNATURE_HEADERS:
            if (value := request.headers.get(name)) is not None:
                header_signature = value
                break

    enforce = settings.is_production or bool(settings.webhook_hmac_secret)
    if enforce and not verify_webhook_signature(body, header_signature):
        logger.warning("Rejected webhook with bad signature on %s", request.url.path)
        raise AuthenticationError("Invalid webhook signature.")

    return body, _decode(body, request.headers.get("content-type", ""))


def _decode(body: bytes, content_type: str) -> dict[str, Any]:
    """Providers post JSON or ``application/x-www-form-urlencoded``; accept both."""
    if not body:
        return {}
    if "form-urlencoded" in content_type:
        from urllib.parse import parse_qsl

        return dict(parse_qsl(body.decode("utf-8", errors="replace")))
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ValidationError("Webhook body is not valid JSON.") from exc
    if not isinstance(payload, dict):
        raise ValidationError("Webhook body must be a JSON object.")
    return payload


@router.post("/telephony", response_model=WebhookAck)
async def telephony_webhook(
    request: Request,
    session: DbSession,
    provider: Annotated[str | None, Query()] = None,
    signature: Annotated[str | None, Header(alias="X-VoiceDesk-Signature")] = None,
) -> WebhookAck:
    """Primary telephony event sink: inbound ring, answer, hangup, recording.

    Providers retry aggressively, so this is idempotent — a replayed event is
    acknowledged with ``duplicate=true`` and changes nothing.
    """
    _, payload = await _verified_body(request, signature)
    provider_name = (provider or payload.get("provider") or settings.telephony_provider).lower()
    event = get_provider(provider_name).parse_webhook(payload)

    # An inbound ring for a call we have never seen creates the call record.
    if event.call_id is None and not await _known(session, provider_name, event):
        direction = str(payload.get("direction") or payload.get("Direction") or "").lower()
        if direction.startswith("incoming") or direction == "inbound":
            call, _ = await call_service.handle_inbound_call(
                session,
                to_number=event.to_number or "",
                from_number=event.from_number or "",
                provider_call_id=event.provider_call_id,
                provider=provider_name,
                raw=payload,
            )
            logger.info(
                "Inbound webhook routed %s -> call %s",
                mask_phone(event.from_number or ""),
                call.id,
            )
            return WebhookAck(call_id=call.id, status=CallStatus(call.status))

    call, duplicate = await call_service.apply_webhook_event(session, event, provider_name)
    if call is None:
        # Unknown call: acknowledge so the provider stops retrying a lost cause.
        return WebhookAck(received=True)

    # Not gated on `duplicate`: providers finish encoding the audio after they
    # report the hangup, so the callback carrying the URL is usually a repeat of
    # a terminal status we have already applied. Gating on the status made that
    # the one callback we ignored, and the recording was lost for good.
    if event.recording_url:
        await _ingest_recording(session, call, event.recording_url, provider_name)

    return WebhookAck(call_id=call.id, status=CallStatus(call.status), duplicate=duplicate)


@router.post("/call-status", response_model=WebhookAck)
async def call_status_webhook(
    request: Request,
    session: DbSession,
    provider: Annotated[str | None, Query()] = None,
    signature: Annotated[str | None, Header(alias="X-VoiceDesk-Signature")] = None,
) -> WebhookAck:
    """Status-only transitions, for providers that split them onto a second URL."""
    _, payload = await _verified_body(request, signature)
    provider_name = (provider or payload.get("provider") or settings.telephony_provider).lower()
    event = get_provider(provider_name).parse_webhook(payload)

    call, duplicate = await call_service.apply_webhook_event(session, event, provider_name)
    if call is None:
        return WebhookAck(received=True)
    return WebhookAck(call_id=call.id, status=CallStatus(call.status), duplicate=duplicate)


async def _known(session: AsyncSession, provider: str, event: WebhookEvent) -> bool:
    call = await call_service.find_by_provider_call_id(session, provider, event.provider_call_id)
    return call is not None


async def _ingest_recording(session: AsyncSession, call: CallLog, url: str, provider: str) -> None:
    """Pull the recording into our own encrypted bucket; never fail the webhook.

    Redelivery is the norm rather than the exception here, so this returns
    early once the audio is stored. Without that, every retry of a terminal
    callback would download and re-encrypt the same file.
    """
    if await recording_service.has_recording(session, call):
        return
    try:
        await recording_service.ingest_from_provider_url(session, call, url)
    except Exception as exc:
        logger.warning("Recording ingest failed for call %s from %s: %s", call.id, provider, exc)
