"""``/api/v1/calls`` — call lifecycle, transcripts and the live turn endpoint."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Query, Response, status

from app.core.deps import CurrentContext, DbSession, RequireOperator
from app.core.errors import ConflictError, NotFoundError
from app.core.tenancy import get_owned_or_404, tenant_select
from app.models.call import CallRecording, Conversation
from app.models.enums import CallDirection, CallResolution, CallStatus
from app.models.voice_agent import VoiceAgent
from app.schemas.call import (
    CallDetailOut,
    CallOut,
    CallTurnRequest,
    CallTurnResponse,
    ConversationTurnOut,
    HangupRequest,
    InitiateCallRequest,
)
from app.schemas.common import Page
from app.services import call_service
from app.services.conversation_engine import get_engine

router = APIRouter(prefix="/calls", tags=["calls"])


@router.post("/initiate", response_model=CallOut, status_code=status.HTTP_201_CREATED)
async def initiate_call(
    payload: InitiateCallRequest, context: RequireOperator, session: DbSession
) -> CallOut:
    """Place or schedule an outbound call. TRAI rules are enforced first."""
    call = await call_service.initiate_call(session, context.business_id, payload)
    return CallOut.model_validate(call)


@router.get("", response_model=Page[CallOut])
async def list_calls(
    context: CurrentContext,
    session: DbSession,
    agent_id: Annotated[uuid.UUID | None, Query()] = None,
    status_filter: Annotated[CallStatus | None, Query(alias="status")] = None,
    direction: Annotated[CallDirection | None, Query()] = None,
    resolution: Annotated[CallResolution | None, Query()] = None,
    caller_number: Annotated[str | None, Query(max_length=20)] = None,
    date_from: Annotated[datetime | None, Query()] = None,
    date_to: Annotated[datetime | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[CallOut]:
    calls, total = await call_service.list_calls(
        session,
        context.business_id,
        agent_id=agent_id,
        status=status_filter,
        direction=direction,
        resolution=resolution,
        caller_number=caller_number,
        date_from=date_from,
        date_to=date_to,
        limit=limit,
        offset=offset,
    )
    return Page[CallOut](
        items=[CallOut.model_validate(c) for c in calls],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/{call_id}", response_model=CallDetailOut)
async def get_call(
    call_id: uuid.UUID,
    context: CurrentContext,
    session: DbSession,
    include_transcript: Annotated[bool, Query()] = True,
) -> CallDetailOut:
    """Call details with its turn-by-turn transcript and sentiment trajectory."""
    call = await call_service.get_call(session, context.business_id, call_id)
    detail = CallDetailOut.model_validate(call)

    if include_transcript:
        detail.conversations = [
            ConversationTurnOut.model_validate(t)
            for t in await _transcript(session, context.business_id, call_id)
        ]
        detail.sentiment_trajectory = await get_engine().sentiment_trajectory(session, call)

    detail.has_recording = (
        await session.execute(
            tenant_select(CallRecording, context.business_id).where(
                CallRecording.call_id == call_id, CallRecording.purged_at.is_(None)
            )
        )
    ).scalar_one_or_none() is not None
    return detail


@router.get("/{call_id}/transcript", response_model=list[ConversationTurnOut])
async def get_transcript(
    call_id: uuid.UUID, context: CurrentContext, session: DbSession
) -> list[ConversationTurnOut]:
    await call_service.get_call(session, context.business_id, call_id)
    turns = await _transcript(session, context.business_id, call_id)
    return [ConversationTurnOut.model_validate(t) for t in turns]


@router.post("/{call_id}/turn", response_model=CallTurnResponse)
async def process_turn(
    call_id: uuid.UUID,
    payload: CallTurnRequest,
    context: RequireOperator,
    session: DbSession,
) -> CallTurnResponse:
    """Feed one transcribed caller utterance to the conversation engine.

    This is the seam the media pipeline drives: ASR posts here, and the reply
    is what TTS speaks back to the caller.
    """
    call = await call_service.get_call(session, context.business_id, call_id)
    if call.status in {s.value for s in CallStatus.terminal()}:
        raise ConflictError(f"Call is {call.status}; no further turns can be processed.")
    if call.agent_id is None:
        raise ConflictError("This call has no voice agent assigned.")

    agent = await get_owned_or_404(
        session, VoiceAgent, call.agent_id, context.business_id, label="Agent"
    )
    result = await get_engine().process_turn(
        session, call, agent, payload.utterance, asr_confidence=payload.asr_confidence
    )
    return CallTurnResponse(
        reply=result.reply,
        language=result.language,
        confidence=result.confidence,
        node_id=result.node_id,
        detected_intent=result.detected_intent,
        sentiment=result.sentiment,
        sentiment_score=result.sentiment_score,
        should_end_call=result.should_end_call,
        should_handoff=result.should_handoff,
        handoff_reason=result.handoff_reason,
        transfer_to=result.transfer_to,
        latency_ms=result.latency_ms,
        model_used=result.model_used,
    )


@router.post("/{call_id}/greeting", response_model=CallTurnResponse)
async def start_call(
    call_id: uuid.UUID, context: RequireOperator, session: DbSession
) -> CallTurnResponse:
    """The agent's opening utterance: recording consent plus greeting."""
    call = await call_service.get_call(session, context.business_id, call_id)
    if call.agent_id is None:
        raise ConflictError("This call has no voice agent assigned.")
    agent = await get_owned_or_404(
        session, VoiceAgent, call.agent_id, context.business_id, label="Agent"
    )
    result = await get_engine().start_call(session, call, agent)
    return CallTurnResponse(
        reply=result.reply,
        language=result.language,
        confidence=result.confidence,
        node_id=result.node_id,
        detected_intent=result.detected_intent,
        sentiment=result.sentiment,
        sentiment_score=result.sentiment_score,
        should_end_call=result.should_end_call,
        should_handoff=result.should_handoff,
        handoff_reason=result.handoff_reason,
        transfer_to=result.transfer_to,
        latency_ms=result.latency_ms,
        model_used=result.model_used,
    )


@router.post("/{call_id}/hangup", response_model=CallOut)
async def hangup(
    call_id: uuid.UUID,
    payload: HangupRequest,
    context: RequireOperator,
    session: DbSession,
) -> CallOut:
    call = await call_service.hangup_call(
        session, context.business_id, call_id, reason=payload.reason
    )
    return CallOut.model_validate(call)


@router.post("/{call_id}/summarise", response_model=CallOut)
async def summarise(call_id: uuid.UUID, context: RequireOperator, session: DbSession) -> CallOut:
    """Generate the post-call summary and roll sentiment up onto the call."""
    call = await call_service.get_call(session, context.business_id, call_id)
    summary, sentiment, score = await get_engine().summarise_call(session, call)
    if not summary:
        raise NotFoundError("This call has no transcript to summarise.")
    call.summary = summary
    call.sentiment = sentiment
    call.sentiment_score = score
    await session.flush()
    return CallOut.model_validate(call)


@router.delete("/{call_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_call(call_id: uuid.UUID, context: RequireOperator, session: DbSession) -> Response:
    """Soft delete — call records are retained for compliance."""
    await call_service.soft_delete_call(session, context.business_id, call_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


async def _transcript(
    session: DbSession, business_id: uuid.UUID, call_id: uuid.UUID
) -> list[Conversation]:
    result = await session.execute(
        tenant_select(Conversation, business_id)
        .where(Conversation.call_id == call_id)
        .order_by(Conversation.turn_index)
    )
    return list(result.scalars().all())
