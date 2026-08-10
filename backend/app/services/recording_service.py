"""Call recording ingestion, playback and retention."""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import ConflictError, NotFoundError
from app.core.logging import get_logger
from app.core.tenancy import tenant_select
from app.models.business import Business
from app.models.call import CallLog, CallRecording
from app.services.storage import StorageService, get_storage
from app.services.telephony import get_provider

logger = get_logger(__name__)

#: Per-business override key inside ``Business.settings_json``.
RETENTION_SETTING = "recording_retention_days"


def retention_days_for(business: Business | None) -> int:
    if business is None:
        return settings.recording_retention_days
    configured = (business.settings_json or {}).get(RETENTION_SETTING)
    try:
        days = int(configured)
    except (TypeError, ValueError):
        return settings.recording_retention_days
    return days if days > 0 else settings.recording_retention_days


async def store_recording(
    session: AsyncSession,
    call: CallLog,
    audio: bytes,
    *,
    content_type: str = "audio/ogg",
    audio_format: str = "opus",
    duration_sec: int | None = None,
    storage: StorageService | None = None,
) -> CallRecording:
    """Encrypt and upload call audio, then record its metadata.

    Re-ingesting audio for a call that already has a recording overwrites the
    object in place, so a provider retry does not create orphaned rows.
    """
    if not audio:
        raise ConflictError("Recording payload is empty.")

    service = storage or get_storage()
    key = service.build_key(call.business_id, call.id, audio_format)
    stored = service.upload(
        key,
        audio,
        content_type=content_type,
        encrypt=True,
        metadata={"call_id": str(call.id), "business_id": str(call.business_id)},
    )

    business = await session.get(Business, call.business_id)
    expires_at = datetime.now(UTC) + timedelta(days=retention_days_for(business))

    existing = (
        await session.execute(
            select(CallRecording).where(
                CallRecording.call_id == call.id,
                CallRecording.business_id == call.business_id,
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
        existing.expires_at = expires_at
        existing.purged_at = None
        existing.deleted_at = None
        await session.flush()
        return existing

    recording = CallRecording(
        business_id=call.business_id,
        call_id=call.id,
        storage_path=stored.key,
        storage_bucket=stored.bucket,
        content_type=content_type,
        format=audio_format,
        duration_sec=duration_sec or call.duration_sec,
        size_bytes=stored.size_bytes,
        encrypted=stored.encrypted,
        encryption_algorithm="AES-256-GCM",
        checksum_sha256=stored.checksum_sha256,
        expires_at=expires_at,
    )
    session.add(recording)
    await session.flush()
    logger.info("Stored recording for call %s (%s bytes)", call.id, stored.size_bytes)
    return recording


async def has_recording(session: AsyncSession, call: CallLog) -> bool:
    """Whether this call's audio is already stored.

    Purged rows count: retention deleted the audio deliberately, and a late
    provider retry must not quietly restore it.
    """
    existing = (
        await session.execute(
            select(CallRecording.id).where(
                CallRecording.call_id == call.id,
                CallRecording.business_id == call.business_id,
            )
        )
    ).first()
    return existing is not None


async def ingest_from_provider_url(
    session: AsyncSession, call: CallLog, url: str, *, storage: StorageService | None = None
) -> CallRecording:
    """Download a provider-hosted recording and re-store it encrypted."""
    provider = get_provider(call.provider)
    audio = await provider.fetch_recording(url)
    return await store_recording(session, call, audio, storage=storage)


async def get_recording(
    session: AsyncSession, business_id: uuid.UUID, call_id: uuid.UUID
) -> CallRecording:
    # Deleted rows are included so a purge can be reported as a purge rather
    # than as a generic miss — the caller needs to know the audio is gone for
    # retention reasons, not that the call never had a recording.
    recording = (
        await session.execute(
            tenant_select(CallRecording, business_id, include_deleted=True).where(
                CallRecording.call_id == call_id
            )
        )
    ).scalar_one_or_none()
    if recording is None:
        raise NotFoundError("No recording is available for this call.")
    if recording.purged_at is not None:
        raise NotFoundError("This recording has been purged under the retention policy.")
    if recording.deleted_at is not None:
        raise NotFoundError("No recording is available for this call.")
    return recording


async def load_audio(
    session: AsyncSession,
    business_id: uuid.UUID,
    call_id: uuid.UUID,
    *,
    storage: StorageService | None = None,
) -> tuple[bytes, CallRecording]:
    """Fetch and decrypt a recording for playback, verifying its checksum."""
    recording = await get_recording(session, business_id, call_id)
    service = storage or get_storage()
    audio = service.download(recording.storage_path, decrypt=recording.encrypted)

    if recording.checksum_sha256:
        digest = hashlib.sha256(audio).hexdigest()
        if digest != recording.checksum_sha256:
            logger.error("Checksum mismatch for recording %s (call %s)", recording.id, call_id)
            raise ConflictError("Recording failed its integrity check.")
    return audio, recording


async def list_recordings(
    session: AsyncSession, business_id: uuid.UUID, *, limit: int = 50, offset: int = 0
) -> tuple[list[CallRecording], int]:
    """One page of recordings plus the tenant's full count, for pagination."""
    stmt = tenant_select(CallRecording, business_id)
    total = await session.scalar(select(func.count()).select_from(stmt.order_by(None).subquery()))
    result = await session.execute(
        stmt.order_by(CallRecording.created_at.desc()).limit(limit).offset(offset)
    )
    return list(result.scalars().all()), int(total or 0)


async def purge_recording(
    session: AsyncSession,
    recording: CallRecording,
    *,
    now: datetime | None = None,
    storage: StorageService | None = None,
) -> bool:
    """Delete one recording's audio ahead of its retention date.

    The metadata row is kept (soft deleted, ``purged_at`` set) so the audit
    trail still shows that a recording existed.
    """
    if recording.purged_at is not None:
        return False
    moment = now or datetime.now(UTC)
    service = storage or get_storage()
    service.delete(recording.storage_path)
    recording.purged_at = moment
    recording.deleted_at = moment
    await session.flush()
    logger.info("Purged recording %s (call %s)", recording.id, recording.call_id)
    return True


async def purge_expired(
    session: AsyncSession, *, now: datetime | None = None, storage: StorageService | None = None
) -> int:
    """Delete recordings past their retention date. Returns the count purged.

    The metadata row is soft-deleted and marked ``purged_at`` — the audit trail
    that a recording existed is retained even though the audio is gone.
    """
    moment = now or datetime.now(UTC)
    service = storage or get_storage()

    expired = (
        (
            await session.execute(
                select(CallRecording).where(
                    CallRecording.expires_at.is_not(None),
                    CallRecording.expires_at <= moment,
                    CallRecording.purged_at.is_(None),
                    CallRecording.deleted_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )

    purged = 0
    for recording in expired:
        if service.delete(recording.storage_path):
            recording.purged_at = moment
            recording.deleted_at = moment
            purged += 1
    if purged:
        await session.flush()
        logger.info("Purged %s expired recording(s)", purged)
    return purged
