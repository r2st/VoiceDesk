"""``/api/v1/voicemails`` — voicemail playback, transcripts and triage."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Query, Response, status

from app.core.deps import CurrentContext, DbSession, RequireOperator
from app.schemas.call import VoicemailOut, VoicemailTranscriptRequest
from app.schemas.common import Page
from app.services import voicemail_service

router = APIRouter(prefix="/voicemails", tags=["voicemails"])


@router.get("", response_model=Page[VoicemailOut])
async def list_voicemails(
    context: CurrentContext,
    session: DbSession,
    unheard_only: Annotated[bool, Query()] = False,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[VoicemailOut]:
    voicemails, total = await voicemail_service.list_voicemails(
        session, context.business_id, unheard_only=unheard_only, limit=limit, offset=offset
    )
    return Page[VoicemailOut](
        items=[VoicemailOut.model_validate(v) for v in voicemails],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/{voicemail_id}", response_model=VoicemailOut)
async def get_voicemail(
    voicemail_id: uuid.UUID, context: CurrentContext, session: DbSession
) -> VoicemailOut:
    voicemail = await voicemail_service.get_voicemail(session, context.business_id, voicemail_id)
    return VoicemailOut.model_validate(voicemail)


@router.get("/{voicemail_id}/stream")
async def stream_voicemail(
    voicemail_id: uuid.UUID, context: CurrentContext, session: DbSession
) -> Response:
    audio, voicemail = await voicemail_service.load_audio(
        session, context.business_id, voicemail_id
    )
    return Response(
        content=audio,
        media_type=voicemail.content_type,
        headers={
            "Content-Disposition": (
                f'inline; filename="voicemail-{voicemail_id}.{voicemail.format}"'
            ),
            "Content-Length": str(len(audio)),
            "Cache-Control": "private, no-store",
        },
    )


@router.post("/{voicemail_id}/listen", response_model=VoicemailOut)
async def listen(
    voicemail_id: uuid.UUID, context: RequireOperator, session: DbSession
) -> VoicemailOut:
    """Mark a voicemail as heard. Idempotent — the first listen wins the timestamp."""
    voicemail = await voicemail_service.mark_listened(
        session, context.business_id, voicemail_id, context.user_id
    )
    return VoicemailOut.model_validate(voicemail)


@router.post("/{voicemail_id}/transcript", response_model=VoicemailOut)
async def set_transcript(
    voicemail_id: uuid.UUID,
    payload: VoicemailTranscriptRequest,
    context: RequireOperator,
    session: DbSession,
) -> VoicemailOut:
    """Attach a transcript produced by ASR run against the stored audio."""
    voicemail = await voicemail_service.set_transcript(
        session, context.business_id, voicemail_id, payload.transcript
    )
    return VoicemailOut.model_validate(voicemail)


@router.delete("/{voicemail_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_voicemail(
    voicemail_id: uuid.UUID, context: RequireOperator, session: DbSession
) -> Response:
    await voicemail_service.delete_voicemail(session, context.business_id, voicemail_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
