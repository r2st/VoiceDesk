"""Live call monitoring and supervisor takeover (design doc §4.3).

Two responsibilities:

* answering "what is happening right now" for the dashboard's live view, and
* arbitrating who is speaking for the business on a call — the AI agent or a
  human supervisor who has taken it over.

Takeover is deliberately a database fact rather than an in-memory flag. The
supervisor's browser talks to whichever replica terminates their WebSocket
while the call's media pipeline talks to a different one, so the only place
both can agree on who holds the call is the shared row.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import TenantContext
from app.core.errors import ConflictError, NotFoundError, PermissionError_
from app.core.events import EventType, emit
from app.core.logging import get_logger
from app.core.tenancy import get_owned_or_404, tenant_select
from app.models.call import CallLog, CallTakeover, Conversation
from app.models.enums import CallStatus, Language, SpeakerRole, UserRole
from app.services.conversation_engine import CallState

logger = get_logger(__name__)

#: Statuses that put a call on the live board. ``QUEUED`` is included so a
#: supervisor sees an outbound call before it starts ringing.
LIVE_STATUSES = (CallStatus.QUEUED, CallStatus.RINGING, CallStatus.IN_PROGRESS)

#: Turns fetched for the snapshot a dashboard receives when it connects.
SNAPSHOT_TURNS = 50

#: Roles that may release a call another supervisor is holding — someone has to
#: be able to clear a takeover left open by a supervisor who closed their laptop.
OVERRIDE_ROLES = frozenset({UserRole.OWNER, UserRole.ADMIN})


@dataclass(slots=True)
class LiveCall:
    """A call currently on the board, with just enough context to triage it."""

    call: CallLog
    turn_count: int
    last_turn: Conversation | None
    takeover: CallTakeover | None

    @property
    def is_supervised(self) -> bool:
        return self.takeover is not None


# --------------------------------------------------------------------------- #
# The live board
# --------------------------------------------------------------------------- #
async def list_live_calls(session: AsyncSession, business_id: uuid.UUID) -> list[LiveCall]:
    """Every non-terminal call for this tenant, newest first."""
    result = await session.execute(
        tenant_select(CallLog, business_id)
        .where(CallLog.status.in_([s.value for s in LIVE_STATUSES]))
        .order_by(CallLog.created_at.desc())
    )
    calls = list(result.scalars().all())
    if not calls:
        return []

    call_ids = [c.id for c in calls]
    counts = await _turn_counts(session, business_id, call_ids)
    last_turns = await _last_turns(session, business_id, call_ids)
    takeovers = {t.call_id: t for t in await _active_takeovers(session, business_id, call_ids)}

    return [
        LiveCall(
            call=call,
            turn_count=counts.get(call.id, 0),
            last_turn=last_turns.get(call.id),
            takeover=takeovers.get(call.id),
        )
        for call in calls
    ]


async def call_snapshot(
    session: AsyncSession, business_id: uuid.UUID, call_id: uuid.UUID
) -> tuple[CallLog, list[Conversation], CallTakeover | None]:
    """The state a newly connected dashboard needs before it starts streaming.

    Without this, a supervisor opening a call that is already ten turns in
    would see an empty pane until the caller happened to speak again.
    """
    call = await get_owned_or_404(session, CallLog, call_id, business_id, label="Call")
    result = await session.execute(
        tenant_select(Conversation, business_id)
        .where(Conversation.call_id == call_id)
        .order_by(Conversation.turn_index.desc())
        .limit(SNAPSHOT_TURNS)
    )
    turns = list(reversed(result.scalars().all()))
    return call, turns, await active_takeover(session, business_id, call_id)


# --------------------------------------------------------------------------- #
# Takeover lifecycle
# --------------------------------------------------------------------------- #
async def active_takeover(
    session: AsyncSession, business_id: uuid.UUID, call_id: uuid.UUID
) -> CallTakeover | None:
    """The open takeover row for a call, if a human currently holds it."""
    result = await session.execute(
        tenant_select(CallTakeover, business_id).where(
            CallTakeover.call_id == call_id, CallTakeover.ended_at.is_(None)
        )
    )
    return result.scalar_one_or_none()


async def _active_takeovers(
    session: AsyncSession, business_id: uuid.UUID, call_ids: list[uuid.UUID]
) -> list[CallTakeover]:
    result = await session.execute(
        tenant_select(CallTakeover, business_id).where(
            CallTakeover.call_id.in_(call_ids), CallTakeover.ended_at.is_(None)
        )
    )
    return list(result.scalars().all())


async def start_takeover(
    session: AsyncSession,
    context: TenantContext,
    call_id: uuid.UUID,
    *,
    reason: str | None = None,
) -> CallTakeover:
    """Hand control of a live call to the requesting supervisor."""
    call = await get_owned_or_404(session, CallLog, call_id, context.business_id, label="Call")
    if call.is_terminal:
        raise ConflictError(f"Call is {call.status}; it can no longer be taken over.")

    existing = await active_takeover(session, context.business_id, call_id)
    if existing is not None:
        if existing.supervisor_user_id == context.user_id:
            return existing
        raise ConflictError("Another supervisor is already handling this call.")

    takeover = CallTakeover(
        business_id=context.business_id,
        call_id=call_id,
        supervisor_user_id=context.user_id,
        reason=reason,
        started_at=datetime.now(UTC),
    )
    session.add(takeover)
    try:
        await session.flush()
    except IntegrityError as exc:
        # Two supervisors clicked at the same moment; the partial unique index
        # picked a winner and this request is the loser.
        await session.rollback()
        raise ConflictError("Another supervisor is already handling this call.") from exc

    await emit(
        EventType.TAKEOVER_STARTED,
        context.business_id,
        call_id=call_id,
        takeover_id=str(takeover.id),
        supervisor_user_id=str(context.user_id),
        supervisor_email=context.email,
        reason=reason,
    )
    logger.info("Call %s taken over by %s", call_id, context.email)
    return takeover


async def end_takeover(
    session: AsyncSession,
    context: TenantContext,
    call_id: uuid.UUID,
    *,
    return_to_ai: bool = True,
) -> CallTakeover:
    """Release the call. The AI resumes on the next caller utterance."""
    takeover = await active_takeover(session, context.business_id, call_id)
    if takeover is None:
        raise NotFoundError("This call is not currently under human control.")
    if takeover.supervisor_user_id != context.user_id and context.role not in OVERRIDE_ROLES:
        raise PermissionError_("Only the supervisor holding this call can release it.")

    takeover.ended_at = datetime.now(UTC)
    takeover.returned_to_ai = return_to_ai
    await session.flush()

    await emit(
        EventType.TAKEOVER_ENDED,
        context.business_id,
        call_id=call_id,
        takeover_id=str(takeover.id),
        supervisor_user_id=str(takeover.supervisor_user_id),
        returned_to_ai=return_to_ai,
        turns_spoken=takeover.turns_spoken,
    )
    return takeover


async def release_for_call_end(session: AsyncSession, call: CallLog) -> None:
    """Close any open takeover when the call itself ends.

    Called from the call lifecycle rather than by a supervisor, so it does not
    check who is asking — the call is over either way, and leaving the row open
    would block the next takeover of a re-used call id and skew the audit.
    """
    takeover = await active_takeover(session, call.business_id, call.id)
    if takeover is None:
        return
    takeover.ended_at = datetime.now(UTC)
    takeover.returned_to_ai = False
    await session.flush()
    await emit(
        EventType.TAKEOVER_ENDED,
        call.business_id,
        call_id=call.id,
        takeover_id=str(takeover.id),
        supervisor_user_id=str(takeover.supervisor_user_id),
        returned_to_ai=False,
        turns_spoken=takeover.turns_spoken,
    )


# --------------------------------------------------------------------------- #
# Speaking as the human
# --------------------------------------------------------------------------- #
async def speak(
    session: AsyncSession,
    context: TenantContext,
    call_id: uuid.UUID,
    text: str,
    *,
    language: Language | None = None,
) -> Conversation:
    """Say something to the caller as the human supervisor.

    The utterance is persisted as a ``HUMAN`` turn so the transcript, the
    recording and the post-call summary all reflect that a person spoke. What
    the media edge does with the returned text — TTS it, or bridge the
    supervisor's own audio — is the transport's business, not ours.
    """
    call = await get_owned_or_404(session, CallLog, call_id, context.business_id, label="Call")
    if call.is_terminal:
        raise ConflictError(f"Call is {call.status}; nothing further can be said on it.")

    takeover = await active_takeover(session, context.business_id, call_id)
    if takeover is None:
        raise ConflictError("Take the call over before speaking on it.")
    if takeover.supervisor_user_id != context.user_id:
        raise PermissionError_("Another supervisor is holding this call.")

    state = CallState.from_call(call)
    turn = Conversation(
        business_id=context.business_id,
        call_id=call_id,
        turn_index=state.turn_index,
        role=SpeakerRole.HUMAN,
        content=text,
        language=(language or Language(call.language or Language.HINDI)).value,
        confidence=1.0,
        flow_node_id=state.current_node,
        metadata_json={"supervisor_user_id": str(context.user_id), "email": context.email},
    )
    session.add(turn)
    state.turn_index += 1
    state.save_to(call)
    takeover.turns_spoken += 1
    await session.flush()

    await emit(
        EventType.TRANSCRIPT_TURN,
        context.business_id,
        call_id=call_id,
        turn_index=turn.turn_index,
        role=SpeakerRole.HUMAN.value,
        content=text,
        language=turn.language,
        confidence=1.0,
        supervisor_email=context.email,
    )
    return turn


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
async def _turn_counts(
    session: AsyncSession, business_id: uuid.UUID, call_ids: list[uuid.UUID]
) -> dict[uuid.UUID, int]:
    result = await session.execute(
        select(Conversation.call_id, func.count())
        .where(
            Conversation.call_id.in_(call_ids),
            Conversation.business_id == business_id,
            Conversation.deleted_at.is_(None),
        )
        .group_by(Conversation.call_id)
    )
    return {row[0]: int(row[1]) for row in result.all()}


async def _last_turns(
    session: AsyncSession, business_id: uuid.UUID, call_ids: list[uuid.UUID]
) -> dict[uuid.UUID, Conversation]:
    """The newest turn of each call, in one round trip.

    The live board shows a dozen calls at once; fetching each call's last turn
    on its own would put a query per row behind a page the dashboard polls.
    """
    newest = (
        select(
            Conversation.call_id.label("call_id"),
            func.max(Conversation.turn_index).label("turn_index"),
        )
        .where(
            Conversation.call_id.in_(call_ids),
            Conversation.business_id == business_id,
            Conversation.deleted_at.is_(None),
        )
        .group_by(Conversation.call_id)
        .subquery()
    )
    result = await session.execute(
        tenant_select(Conversation, business_id).join(
            newest,
            (Conversation.call_id == newest.c.call_id)
            & (Conversation.turn_index == newest.c.turn_index),
        )
    )
    return {turn.call_id: turn for turn in result.scalars().all()}
