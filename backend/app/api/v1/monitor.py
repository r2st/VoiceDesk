"""``/api/v1/monitor`` — the live call board, its WebSocket, and takeover.

Design doc §4.3: supervisors watch calls as they happen and can take one over
with a single click. The REST half answers "what is live right now" and drives
the takeover controls; the WebSocket half streams transcript turns and status
changes as they are produced.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect, status

from app.core.deps import CurrentContext, DbSession, RequireOperator, TenantContext
from app.core.errors import AuthenticationError, VoiceDeskError
from app.core.events import watch
from app.core.logging import get_logger
from app.core.security import decode_token
from app.core.timeutil import ensure_utc
from app.db.session import get_sessionmaker
from app.models.business import User
from app.models.enums import UserRole
from app.schemas.call import CallOut, ConversationTurnOut
from app.schemas.monitoring import (
    CallSnapshotOut,
    LiveCallOut,
    ReleaseRequest,
    SupervisorSayRequest,
    TakeoverOut,
    TakeoverRequest,
)
from app.services import monitoring

logger = get_logger(__name__)

router = APIRouter(prefix="/monitor", tags=["monitoring"])

#: Sent when the tenant's channel is quiet, so a dead connection is noticed
#: rather than lingering until the proxy's read timeout closes it.
KEEPALIVE_SECONDS = 20.0

#: Roles allowed to watch and take over calls.
MONITOR_ROLES = (UserRole.OWNER, UserRole.ADMIN, UserRole.SUPERVISOR)


# --------------------------------------------------------------------------- #
# REST
# --------------------------------------------------------------------------- #
@router.get("/live", response_model=list[LiveCallOut])
async def live_calls(context: CurrentContext, session: DbSession) -> list[LiveCallOut]:
    """Every call currently in flight for this business."""
    return [
        _to_live_out(item)
        for item in await monitoring.list_live_calls(session, context.business_id)
    ]


@router.get("/calls/{call_id}", response_model=CallSnapshotOut)
async def call_snapshot(
    call_id: uuid.UUID, context: CurrentContext, session: DbSession
) -> CallSnapshotOut:
    """The transcript so far plus who currently controls the call."""
    call, turns, takeover = await monitoring.call_snapshot(session, context.business_id, call_id)
    return CallSnapshotOut(
        call=CallOut.model_validate(call),
        turns=[ConversationTurnOut.model_validate(t) for t in turns],
        takeover=TakeoverOut.model_validate(takeover) if takeover else None,
    )


@router.post("/calls/{call_id}/takeover", response_model=TakeoverOut)
async def take_over(
    call_id: uuid.UUID,
    payload: TakeoverRequest,
    context: RequireOperator,
    session: DbSession,
) -> TakeoverOut:
    """Take the call off the AI. Subsequent caller turns wait for a human."""
    takeover = await monitoring.start_takeover(session, context, call_id, reason=payload.reason)
    return TakeoverOut.model_validate(takeover)


@router.post("/calls/{call_id}/release", response_model=TakeoverOut)
async def release(
    call_id: uuid.UUID,
    payload: ReleaseRequest,
    context: RequireOperator,
    session: DbSession,
) -> TakeoverOut:
    """Hand the call back to the AI agent."""
    takeover = await monitoring.end_takeover(
        session, context, call_id, return_to_ai=payload.return_to_ai
    )
    return TakeoverOut.model_validate(takeover)


@router.post(
    "/calls/{call_id}/say",
    response_model=ConversationTurnOut,
    status_code=status.HTTP_201_CREATED,
)
async def say(
    call_id: uuid.UUID,
    payload: SupervisorSayRequest,
    context: RequireOperator,
    session: DbSession,
) -> ConversationTurnOut:
    """Speak to the caller as the human now handling the call."""
    turn = await monitoring.speak(
        session, context, call_id, payload.text, language=payload.language
    )
    return ConversationTurnOut.model_validate(turn)


# --------------------------------------------------------------------------- #
# WebSocket
# --------------------------------------------------------------------------- #
@router.websocket("/stream")
async def stream(
    websocket: WebSocket,
    token: Annotated[str, Query(description="Access token; browsers cannot set WS headers.")],
    call_id: Annotated[uuid.UUID | None, Query()] = None,
) -> None:
    """Stream this tenant's live events, optionally narrowed to one call.

    The token arrives as a query parameter because the browser WebSocket API
    has no way to set an ``Authorization`` header. It is verified against the
    database exactly like a REST request — a signed token for a deactivated
    user is still rejected.
    """
    try:
        context = await _authenticate(token)
    except VoiceDeskError as exc:
        # The handshake has not completed, so there is no socket to send an
        # error frame on; the close code is the only channel available.
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason=exc.message)
        return

    await websocket.accept()
    logger.info("Monitor stream opened by %s (call=%s)", context.email, call_id)

    try:
        if call_id is not None:
            await _send_snapshot(websocket, context, call_id)
        await _relay(websocket, context, call_id)
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("Monitor stream failed for %s", context.email)
        try:
            await websocket.close(code=status.WS_1011_INTERNAL_ERROR)
        except RuntimeError:
            # Already closed from the other end; nothing left to tell it.
            pass
    finally:
        logger.info("Monitor stream closed for %s", context.email)


async def _relay(websocket: WebSocket, context: TenantContext, call_id: uuid.UUID | None) -> None:
    """Pump tenant events to the socket until the client goes away."""
    async with watch(context.business_id) as events:
        while True:
            try:
                event = await asyncio.wait_for(events.__anext__(), timeout=KEEPALIVE_SECONDS)
            except TimeoutError:
                # A quiet channel still has to prove the socket is alive.
                await websocket.send_json({"type": "ping", "at": datetime.now(UTC).isoformat()})
                continue
            except StopAsyncIteration:
                break

            # One channel carries the whole tenant, so a pane pinned to a single
            # call filters here rather than holding its own subscription.
            if call_id is not None and event.call_id != call_id:
                continue
            await websocket.send_json(event.to_dict())


async def _send_snapshot(websocket: WebSocket, context: TenantContext, call_id: uuid.UUID) -> None:
    """Open with the current state so the pane is never blank on connect."""
    async with get_sessionmaker()() as session:
        call, turns, takeover = await monitoring.call_snapshot(
            session, context.business_id, call_id
        )
        await websocket.send_json(
            {
                "type": "snapshot",
                "call_id": str(call_id),
                "data": CallSnapshotOut(
                    call=CallOut.model_validate(call),
                    turns=[ConversationTurnOut.model_validate(t) for t in turns],
                    takeover=TakeoverOut.model_validate(takeover) if takeover else None,
                ).model_dump(mode="json"),
            }
        )


async def _authenticate(token: str) -> TenantContext:
    """Resolve a query-string access token into a monitoring principal.

    Uses its own session: WebSocket routes get no request-scoped dependency,
    and the connection outlives any transaction we would borrow.
    """
    payload = decode_token(token, expected_type="access")
    try:
        user_id = uuid.UUID(payload["sub"])
        business_id = uuid.UUID(payload["business_id"])
    except (KeyError, ValueError) as exc:
        raise AuthenticationError("Malformed token claims.") from exc

    async with get_sessionmaker()() as session:
        user = await session.get(User, user_id)

    if user is None or user.deleted_at is not None or not user.is_active:
        raise AuthenticationError("User is no longer active.")
    if user.business_id != business_id:
        raise AuthenticationError("Token does not match the user's business.")
    if UserRole(user.role) not in MONITOR_ROLES:
        raise AuthenticationError("This role cannot monitor live calls.")

    return TenantContext(
        user_id=user.id,
        business_id=user.business_id,
        role=UserRole(user.role),
        email=user.email,
    )


# --------------------------------------------------------------------------- #
# Serialisation
# --------------------------------------------------------------------------- #
def _to_live_out(item: monitoring.LiveCall) -> LiveCallOut:
    call = item.call
    reference = call.answered_at or call.started_at
    # SQLite hands back naive datetimes, so re-stamp before subtracting.
    elapsed = int((datetime.now(UTC) - ensure_utc(reference)).total_seconds()) if reference else 0
    return LiveCallOut(
        call_id=call.id,
        agent_id=call.agent_id,
        direction=call.direction,
        status=call.status,
        caller_number=call.caller_number,
        callee_number=call.callee_number,
        language=call.language,
        sentiment=call.sentiment,
        sentiment_score=call.sentiment_score,
        started_at=call.started_at,
        answered_at=call.answered_at,
        elapsed_sec=max(0, elapsed),
        turn_count=item.turn_count,
        last_speaker=item.last_turn.role if item.last_turn else None,
        last_utterance=item.last_turn.content if item.last_turn else None,
        takeover=TakeoverOut.model_validate(item.takeover) if item.takeover else None,
    )
