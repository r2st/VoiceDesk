"""``/api/v1/recordings`` — encrypted call recording access (design doc §2.4)."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Query, Response, status

from app.core.deps import CurrentContext, DbSession, RequireOperator
from app.schemas.call import RecordingOut, RecordingUrlOut
from app.schemas.common import Page
from app.services import recording_service
from app.services.storage import get_storage

router = APIRouter(prefix="/recordings", tags=["recordings"])


@router.get("", response_model=Page[RecordingOut])
async def list_recordings(
    context: CurrentContext,
    session: DbSession,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[RecordingOut]:
    recordings = await recording_service.list_recordings(
        session, context.business_id, limit=limit, offset=offset
    )
    return Page[RecordingOut](
        items=[RecordingOut.model_validate(r) for r in recordings],
        total=len(recordings) + offset,
        limit=limit,
        offset=offset,
    )


@router.get("/{call_id}", response_model=RecordingOut)
async def get_recording(
    call_id: uuid.UUID, context: CurrentContext, session: DbSession
) -> RecordingOut:
    """Metadata only — the audio itself is served by ``/stream``."""
    recording = await recording_service.get_recording(session, context.business_id, call_id)
    return RecordingOut.model_validate(recording)


@router.get("/{call_id}/stream")
async def stream_recording(
    call_id: uuid.UUID, context: CurrentContext, session: DbSession
) -> Response:
    """Decrypt and return the audio.

    Playback goes through the API rather than a presigned URL because objects
    are AES-256 encrypted at rest — a direct S3 fetch would yield ciphertext.
    """
    audio, recording = await recording_service.load_audio(session, context.business_id, call_id)
    return Response(
        content=audio,
        media_type=recording.content_type,
        headers={
            "Content-Disposition": f'inline; filename="call-{call_id}.{recording.format}"',
            "Content-Length": str(len(audio)),
            "Cache-Control": "private, no-store",
        },
    )


@router.get("/{call_id}/url", response_model=RecordingUrlOut)
async def presigned_url(
    call_id: uuid.UUID,
    context: RequireOperator,
    session: DbSession,
    expires_in: Annotated[int, Query(ge=60, le=3600)] = 900,
) -> RecordingUrlOut:
    """A short-lived direct-to-storage URL, for compliance export tooling."""
    recording = await recording_service.get_recording(session, context.business_id, call_id)
    url = get_storage().presigned_url(recording.storage_path, expires_seconds=expires_in)
    return RecordingUrlOut(
        url=url,
        expires_in=expires_in,
        encrypted=recording.encrypted,
        note=(
            "The object is AES-256 encrypted at rest; this URL yields ciphertext. "
            "Use /stream for playable audio."
            if recording.encrypted
            else None
        ),
    )


@router.delete("/{call_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_recording(
    call_id: uuid.UUID, context: RequireOperator, session: DbSession
) -> Response:
    """Remove the audio object early; the metadata row is retained for audit."""
    recording = await recording_service.get_recording(session, context.business_id, call_id)
    await recording_service.purge_recording(session, recording)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
