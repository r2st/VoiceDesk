"""Call orchestration: initiation, webhook ingestion and lifecycle transitions."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import ComplianceError, ConflictError, NotFoundError, ValidationError
from app.core.events import EventType, emit
from app.core.logging import get_logger, mask_phone
from app.core.tenancy import get_owned_or_404, tenant_select
from app.models.call import CallLog, PhoneNumber
from app.models.enums import (
    AgentStatus,
    CallDirection,
    CallResolution,
    CallStatus,
    PhoneNumberStatus,
)
from app.models.voice_agent import VoiceAgent
from app.schemas.call import InitiateCallRequest
from app.services import billing_service, compliance, entitlements
from app.services.telephony import CallRequest, WebhookEvent, get_provider

logger = get_logger(__name__)

#: Statuses a call can still move out of.
NON_TERMINAL = {CallStatus.QUEUED, CallStatus.RINGING, CallStatus.IN_PROGRESS}


async def initiate_call(
    session: AsyncSession, business_id: uuid.UUID, payload: InitiateCallRequest
) -> CallLog:
    """Place (or schedule) an outbound call after clearing TRAI rules."""
    # Checked before anything else: a suspended tenant should be told its
    # account is the problem, not sent away with a complaint about its agent.
    await entitlements.require_calling_entitlement(session, business_id)

    agent = await get_owned_or_404(
        session, VoiceAgent, payload.agent_id, business_id, label="Agent"
    )
    if agent.status != AgentStatus.ACTIVE:
        raise ValidationError(
            f"Agent '{agent.name}' is {agent.status}; activate it before placing calls."
        )

    from_number = await _resolve_outbound_number(
        session, business_id, agent.id, payload.from_number_id
    )

    decision = await compliance.check_outbound_call(
        session,
        to_number=payload.to_number,
        business_id=business_id,
        caller_id=from_number.number,
        scheduled_at=payload.scheduled_at,
    )

    call = CallLog(
        business_id=business_id,
        agent_id=agent.id,
        phone_number_id=from_number.id,
        direction=CallDirection.OUTBOUND,
        status=CallStatus.QUEUED,
        caller_number=payload.to_number,  # the human on the call
        callee_number=from_number.number,  # the VoiceDesk line
        provider=from_number.provider,
        scheduled_at=payload.scheduled_at,
        dnd_checked=settings.trai_dnd_check_enabled,
        metadata_json={
            "state": {"variables": payload.variables, "turn_index": 0},
            "request": payload.metadata,
        },
    )

    if not decision.allowed:
        call.status = (
            CallStatus.BLOCKED_DND if decision.code == "dnd_blocked" else CallStatus.BLOCKED_HOURS
        )
        call.resolution = CallResolution.UNRESOLVED
        call.error_code = decision.code
        call.error_message = decision.reason
        call.ended_at = datetime.now(UTC)
        session.add(call)
        await session.flush()
        raise ComplianceError(
            decision.reason or "Call blocked by compliance rules.",
            details={"call_id": str(call.id), "code": decision.code, **(decision.details or {})},
        )

    session.add(call)
    await session.flush()

    # A scheduled call is handed to the queue rather than dialled now.
    if payload.scheduled_at and payload.scheduled_at > datetime.now(UTC):
        logger.info("Call %s scheduled for %s", call.id, payload.scheduled_at.isoformat())
        return call

    return await dial(session, call, agent)


async def dial(session: AsyncSession, call: CallLog, agent: VoiceAgent) -> CallLog:
    """Hand a queued call to the telephony provider."""
    provider = get_provider(call.provider)
    result = await provider.initiate_call(
        CallRequest(
            to_number=call.caller_number,
            from_number=call.callee_number,
            callback_url=f"{settings.telephony_callback_base_url}/api/v1/webhooks/telephony",
            call_id=str(call.id),
            record=agent.recording_enabled,
            caller_id=call.callee_number,
            metadata={"business_id": str(call.business_id), "agent_id": str(agent.id)},
        )
    )

    call.provider_call_id = result.provider_call_id or None
    call.status = result.status
    call.started_at = datetime.now(UTC)
    if result.status == CallStatus.FAILED:
        call.error_message = (result.error_message or "Provider rejected the call")[:500]
        call.ended_at = datetime.now(UTC)
        call.resolution = CallResolution.UNRESOLVED
    await session.flush()

    logger.info(
        "Dialled %s from %s (call=%s status=%s)",
        mask_phone(call.caller_number),
        mask_phone(call.callee_number),
        call.id,
        call.status,
    )
    return call


async def _resolve_outbound_number(
    session: AsyncSession,
    business_id: uuid.UUID,
    agent_id: uuid.UUID,
    from_number_id: uuid.UUID | None,
) -> PhoneNumber:
    """Pick the line to dial from: the requested one, the agent's, or any active one."""
    if from_number_id is not None:
        number = await get_owned_or_404(
            session, PhoneNumber, from_number_id, business_id, label="Phone number"
        )
        if not number.outbound_enabled:
            raise ValidationError(f"Outbound calling is disabled on {number.number}.")
        if number.status != PhoneNumberStatus.ACTIVE:
            raise ValidationError(f"Number {number.number} is {number.status}, not active.")
        return number

    stmt = (
        tenant_select(PhoneNumber, business_id)
        .where(
            PhoneNumber.status == PhoneNumberStatus.ACTIVE,
            PhoneNumber.outbound_enabled.is_(True),
        )
        .order_by(
            # Prefer a number already bound to this agent.
            (PhoneNumber.agent_id == agent_id).desc(),
            PhoneNumber.created_at,
        )
    )
    number = (await session.execute(stmt.limit(1))).scalar_one_or_none()
    if number is None:
        raise ValidationError(
            "No active outbound phone number is provisioned for this business.",
            details={"hint": "POST /api/v1/phone-numbers/provision"},
        )
    return number


# --------------------------------------------------------------------------- #
# Inbound
# --------------------------------------------------------------------------- #
async def handle_inbound_call(
    session: AsyncSession,
    *,
    to_number: str,
    from_number: str,
    provider_call_id: str,
    provider: str,
    raw: dict[str, Any] | None = None,
) -> tuple[CallLog, VoiceAgent | None]:
    """Route an inbound call to the business and agent that own the dialled number."""
    line = (
        await session.execute(
            select(PhoneNumber).where(
                PhoneNumber.number == to_number,
                PhoneNumber.deleted_at.is_(None),
                PhoneNumber.status == PhoneNumberStatus.ACTIVE,
                PhoneNumber.inbound_enabled.is_(True),
            )
        )
    ).scalar_one_or_none()
    if line is None:
        raise NotFoundError(f"No active VoiceDesk number matches {mask_phone(to_number)}.")

    agent: VoiceAgent | None = None
    if line.agent_id is not None:
        agent = (
            await session.execute(
                tenant_select(VoiceAgent, line.business_id).where(VoiceAgent.id == line.agent_id)
            )
        ).scalar_one_or_none()

    existing = await find_by_provider_call_id(session, provider, provider_call_id)
    if existing is not None:
        return existing, agent

    # Checked only for a genuinely new call. Doing it earlier would mean a
    # tenant whose trial lapses mid-call starts rejecting the webhook replays
    # for the call already in progress.
    await entitlements.require_calling_entitlement(session, line.business_id)

    call = CallLog(
        business_id=line.business_id,
        agent_id=agent.id if agent else None,
        phone_number_id=line.id,
        direction=CallDirection.INBOUND,
        status=CallStatus.RINGING,
        caller_number=from_number,
        callee_number=to_number,
        provider=provider,
        provider_call_id=provider_call_id or None,
        started_at=datetime.now(UTC),
        metadata_json={"state": {"turn_index": 0}, "provider_raw": raw or {}},
    )
    session.add(call)
    await session.flush()
    logger.info("Inbound call %s from %s", call.id, mask_phone(from_number))
    await _publish_status(call, EventType.CALL_STARTED)
    return call, agent


# --------------------------------------------------------------------------- #
# Webhooks
# --------------------------------------------------------------------------- #
async def apply_webhook_event(
    session: AsyncSession, event: WebhookEvent, provider: str
) -> tuple[CallLog | None, bool]:
    """Apply a telephony status event to its call.

    Returns ``(call, duplicate)``. Providers retry aggressively, so this is
    written to be idempotent: replaying a terminal event changes nothing.
    """
    call = await _locate_call(session, event, provider)
    if call is None:
        logger.warning(
            "Webhook for unknown call (provider=%s id=%s)", provider, event.provider_call_id
        )
        return None, False

    if event.status is None:
        # The callback said nothing about the call's state — a recording-ready
        # ping, or a status the provider introduced after this code was written.
        # Its side data is still worth keeping, but it must not decide the
        # call's state: an unrecognised status used to parse as `failed`, which
        # is terminal, so a stray callback ended a live call, stamped it
        # unresolved and metered it for billing.
        if event.provider_call_id and not call.provider_call_id:
            call.provider_call_id = event.provider_call_id
        _attach_event_details(call, event)
        await session.flush()
        return call, False

    if call.status == event.status.value and call.status not in {s.value for s in NON_TERMINAL}:
        return call, True

    # Never move a call backwards out of a terminal state.
    if call.status in {s.value for s in CallStatus.terminal()}:
        return call, True

    previous = call.status
    call.status = event.status
    if event.provider_call_id and not call.provider_call_id:
        call.provider_call_id = event.provider_call_id

    now = datetime.now(UTC)
    if event.status == CallStatus.IN_PROGRESS and call.answered_at is None:
        call.answered_at = now
    if event.status in CallStatus.terminal():
        call.ended_at = now
        call.duration_sec = event.duration_sec or _derive_duration(call, now)
        if event.status != CallStatus.COMPLETED and call.resolution == CallResolution.PENDING:
            call.resolution = CallResolution.UNRESOLVED

    _attach_event_details(call, event)

    await session.flush()

    if event.status in CallStatus.terminal():
        await billing_service.meter_call(session, call)
        await _close_open_takeover(session, call)

    logger.info("Call %s: %s -> %s", call.id, previous, call.status)
    await _publish_status(
        call,
        EventType.CALL_ENDED if call.is_terminal else EventType.CALL_STATUS,
        previous=previous,
    )
    return call, False


def _attach_event_details(call: CallLog, event: WebhookEvent) -> None:
    """Copy the non-status fields of a webhook event onto the call."""
    if event.error_code:
        call.error_code = event.error_code[:80]
    if event.error_message:
        call.error_message = event.error_message[:500]
    if event.recording_url:
        metadata = dict(call.metadata_json or {})
        metadata["recording_url"] = event.recording_url
        call.metadata_json = metadata


async def _locate_call(session: AsyncSession, event: WebhookEvent, provider: str) -> CallLog | None:
    """Find the call by our own id first, then by the provider's."""
    if event.call_id:
        try:
            call = await session.get(CallLog, uuid.UUID(str(event.call_id)))
            if call is not None and call.deleted_at is None:
                return call
        except (ValueError, AttributeError):
            pass
    if event.provider_call_id:
        return await find_by_provider_call_id(session, provider, event.provider_call_id)
    return None


async def find_by_provider_call_id(
    session: AsyncSession, provider: str, provider_call_id: str
) -> CallLog | None:
    if not provider_call_id:
        return None
    return (
        await session.execute(
            select(CallLog).where(
                CallLog.provider == provider,
                CallLog.provider_call_id == provider_call_id,
                CallLog.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()


def _derive_duration(call: CallLog, ended_at: datetime) -> int:
    """Fall back to wall-clock duration when the provider omits one."""
    started = call.answered_at or call.started_at
    if started is None:
        return 0
    if started.tzinfo is None:
        started = started.replace(tzinfo=UTC)
    return max(0, int((ended_at - started).total_seconds()))


# --------------------------------------------------------------------------- #
# Queries and lifecycle
# --------------------------------------------------------------------------- #
async def get_call(session: AsyncSession, business_id: uuid.UUID, call_id: uuid.UUID) -> CallLog:
    return await get_owned_or_404(session, CallLog, call_id, business_id, label="Call")


async def list_calls(
    session: AsyncSession,
    business_id: uuid.UUID,
    *,
    agent_id: uuid.UUID | None = None,
    status: CallStatus | None = None,
    direction: CallDirection | None = None,
    resolution: CallResolution | None = None,
    caller_number: str | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[CallLog], int]:
    stmt = tenant_select(CallLog, business_id)
    if agent_id is not None:
        stmt = stmt.where(CallLog.agent_id == agent_id)
    if status is not None:
        stmt = stmt.where(CallLog.status == status)
    if direction is not None:
        stmt = stmt.where(CallLog.direction == direction)
    if resolution is not None:
        stmt = stmt.where(CallLog.resolution == resolution)
    if caller_number:
        stmt = stmt.where(CallLog.caller_number.contains(caller_number))
    if date_from is not None:
        stmt = stmt.where(CallLog.created_at >= date_from)
    if date_to is not None:
        stmt = stmt.where(CallLog.created_at < date_to)

    total = await session.scalar(select(func.count()).select_from(stmt.order_by(None).subquery()))
    result = await session.execute(
        stmt.order_by(CallLog.created_at.desc()).limit(limit).offset(offset)
    )
    return list(result.scalars().all()), int(total or 0)


async def hangup_call(
    session: AsyncSession, business_id: uuid.UUID, call_id: uuid.UUID, reason: str | None = None
) -> CallLog:
    call = await get_call(session, business_id, call_id)
    if call.status in {s.value for s in CallStatus.terminal()}:
        raise ConflictError(f"Call is already {call.status}.")

    if call.provider_call_id:
        try:
            await get_provider(call.provider).hangup(call.provider_call_id)
        except Exception as exc:
            logger.warning("Provider hangup failed for %s: %s", call.id, exc)

    now = datetime.now(UTC)
    call.status = CallStatus.COMPLETED
    call.ended_at = now
    call.duration_sec = call.duration_sec or _derive_duration(call, now)
    if reason:
        call.error_message = reason[:500]
    if call.resolution == CallResolution.PENDING:
        call.resolution = CallResolution.UNRESOLVED

    await session.flush()
    await billing_service.meter_call(session, call)
    await _close_open_takeover(session, call)
    await _publish_status(call, EventType.CALL_ENDED, previous=CallStatus.IN_PROGRESS)
    return call


async def _publish_status(call: CallLog, event_type: str, *, previous: str | None = None) -> None:
    """Tell the live dashboards where this call now stands (design doc §4.3)."""
    await emit(
        event_type,
        call.business_id,
        call_id=call.id,
        status=call.status,
        previous_status=previous,
        direction=call.direction,
        agent_id=str(call.agent_id) if call.agent_id else None,
        caller_number=mask_phone(call.caller_number),
        duration_sec=call.duration_sec,
        resolution=call.resolution,
    )


async def _close_open_takeover(session: AsyncSession, call: CallLog) -> None:
    """End any supervisor takeover when the call itself ends.

    Imported here rather than at module scope: ``monitoring`` reaches back into
    the conversation engine, and importing it eagerly would tangle the call
    path with the dashboard's.
    """
    from app.services import monitoring

    await monitoring.release_for_call_end(session, call)


async def soft_delete_call(
    session: AsyncSession, business_id: uuid.UUID, call_id: uuid.UUID
) -> None:
    call = await get_call(session, business_id, call_id)
    if call.status in {s.value for s in NON_TERMINAL}:
        raise ConflictError("Cannot delete a call that is still in progress.")
    call.deleted_at = datetime.now(UTC)
    await session.flush()
