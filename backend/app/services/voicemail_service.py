"""Voicemail capture, transcription state and playback.

A caller who reaches an agent that is unavailable — paused, over capacity, or
simply absent from an inbound number — is offered voicemail instead. The audio
is stored the same encrypted way call recordings are (design doc §2.4); this
module only differs in what the row means and its extra transcription and
listened-state fields.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, NotFoundError
from app.core.events import EventType, emit
from app.core.logging import get_logger
from app.core.tenancy import get_owned_or_404, tenant_select
from app.models.call import CallLog, Voicemail
from app.models.enums import VoicemailStatus
from app.services.storage import StorageService, get_storage
from app.services.telephony import get_provider

logger = get_logger(__name__)


def _build_key(business_id: uuid.UUID, call_id: uuid.UUID, extension: str) -> str:
    return f"voicemails/{business_id}/{call_id}.{extension}.enc"


async def store_voicemail(
    session: AsyncSession,
    call: CallLog,
    audio: bytes,
    *,
    content_type: str = "audio/ogg",
    audio_format: str = "opus",
    duration_sec: int | None = None,
    storage: StorageService | None = None,
) -> Voicemail:
    """Encrypt and upload a voicemail message, then record its metadata.

    Re-ingesting audio for a call that already has a voicemail overwrites the
    object in place, mirroring how call recordings absorb a provider retry.
    """
    if not audio:
        raise ConflictError("Voicemail payload is empty.")

    service = storage or get_storage()
    key = _build_key(call.business_id, call.id, audio_format)
    stored = service.upload(
        key,
        audio,
        content_type=content_type,
        encrypt=True,
        metadata={"call_id": str(call.id), "business_id": str(call.business_id)},
    )

    existing = (
        await session.execute(
            select(Voicemail).where(
                Voicemail.call_id == call.id, Voicemail.business_id == call.business_id
            )
        )
    ).scalar_one_or_none()

    if existing is not None:
        existing.storage_path = stored.key
        existing.storage_bucket = stored.bucket
        existing.content_type = content_type
        existing.format = audio_format
        existing.duration_sec = duration_sec or call.duration_sec
        existing.size_bytes = stored.size_bytes
        existing.checksum_sha256 = stored.checksum_sha256
        existing.status = VoicemailStatus.PENDING
        existing.transcript = None
        existing.transcribed_at = None
        existing.listened_at = None
        existing.listened_by = None
        existing.deleted_at = None
        await session.flush()
        voicemail = existing
    else:
        voicemail = Voicemail(
            business_id=call.business_id,
            call_id=call.id,
            phone_number_id=call.phone_number_id,
            caller_number=call.caller_number,
            storage_path=stored.key,
            storage_bucket=stored.bucket,
            content_type=content_type,
            format=audio_format,
            duration_sec=duration_sec or call.duration_sec,
            size_bytes=stored.size_bytes,
            checksum_sha256=stored.checksum_sha256,
            status=VoicemailStatus.PENDING,
        )
        session.add(voicemail)
        await session.flush()

    logger.info("Stored voicemail for call %s (%s bytes)", call.id, stored.size_bytes)
    await emit(
        EventType.VOICEMAIL_RECEIVED,
        call.business_id,
        call_id=call.id,
        voicemail_id=str(voicemail.id),
        caller_number=voicemail.caller_number,
        duration_sec=voicemail.duration_sec,
    )
    return voicemail


async def ingest_from_provider_url(
    session: AsyncSession, call: CallLog, url: str, *, storage: StorageService | None = None
) -> Voicemail:
    """Download a provider-hosted voicemail recording and re-store it encrypted."""
    provider = get_provider(call.provider)
    audio = await provider.fetch_recording(url)
    return await store_voicemail(session, call, audio, storage=storage)


async def get_voicemail(
    session: AsyncSession, business_id: uuid.UUID, voicemail_id: uuid.UUID
) -> Voicemail:
    return await get_owned_or_404(session, Voicemail, voicemail_id, business_id, label="Voicemail")


async def get_voicemail_for_call(
    session: AsyncSession, business_id: uuid.UUID, call_id: uuid.UUID
) -> Voicemail:
    result = await session.execute(
        tenant_select(Voicemail, business_id).where(Voicemail.call_id == call_id)
    )
    voicemail = result.scalar_one_or_none()
    if voicemail is None:
        raise NotFoundError("No voicemail is available for this call.")
    return voicemail


async def list_voicemails(
    session: AsyncSession,
    business_id: uuid.UUID,
    *,
    unheard_only: bool = False,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[Voicemail], int]:
    stmt = tenant_select(Voicemail, business_id)
    if unheard_only:
        stmt = stmt.where(Voicemail.listened_at.is_(None))

    total = await session.scalar(select(func.count()).select_from(stmt.order_by(None).subquery()))
    result = await session.execute(
        stmt.order_by(Voicemail.created_at.desc()).limit(limit).offset(offset)
    )
    return list(result.scalars().all()), int(total or 0)


async def mark_listened(
    session: AsyncSession, business_id: uuid.UUID, voicemail_id: uuid.UUID, user_id: uuid.UUID
) -> Voicemail:
    voicemail = await get_voicemail(session, business_id, voicemail_id)
    if voicemail.listened_at is None:
        voicemail.listened_at = datetime.now(UTC)
        voicemail.listened_by = user_id
        await session.flush()
    return voicemail


async def set_transcript(
    session: AsyncSession, business_id: uuid.UUID, voicemail_id: uuid.UUID, transcript: str
) -> Voicemail:
    """Record a transcript, produced by ASR run against the stored audio."""
    voicemail = await get_voicemail(session, business_id, voicemail_id)
    voicemail.transcript = transcript
    voicemail.status = VoicemailStatus.TRANSCRIBED
    voicemail.transcribed_at = datetime.now(UTC)
    await session.flush()
    return voicemail


async def mark_transcription_failed(
    session: AsyncSession, business_id: uuid.UUID, voicemail_id: uuid.UUID
) -> Voicemail:
    voicemail = await get_voicemail(session, business_id, voicemail_id)
    voicemail.status = VoicemailStatus.FAILED
    await session.flush()
    return voicemail


async def load_audio(
    session: AsyncSession,
    business_id: uuid.UUID,
    voicemail_id: uuid.UUID,
    *,
    storage: StorageService | None = None,
) -> tuple[bytes, Voicemail]:
    voicemail = await get_voicemail(session, business_id, voicemail_id)
    service = storage or get_storage()
    audio = service.download(voicemail.storage_path, decrypt=True)
    return audio, voicemail


async def delete_voicemail(
    session: AsyncSession, business_id: uuid.UUID, voicemail_id: uuid.UUID
) -> None:
    voicemail = await get_voicemail(session, business_id, voicemail_id)
    service = get_storage()
    service.delete(voicemail.storage_path)
    voicemail.deleted_at = datetime.now(UTC)
    await session.flush()
