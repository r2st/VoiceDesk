"""``/api/v1/whatsapp`` — voice-to-WhatsApp handoff (design doc §4.5)."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Query, status

from app.core.deps import CurrentContext, DbSession, RequireOperator
from app.core.tenancy import tenant_select
from app.models.call import WhatsAppHandoff
from app.models.enums import HandoffStatus, Language
from app.schemas.call import HandoffOut, HandoffRequest
from app.schemas.common import Page
from app.services import call_service, whatsapp

router = APIRouter(prefix="/whatsapp", tags=["whatsapp"])


@router.post("/handoff/{call_id}", response_model=HandoffOut, status_code=status.HTTP_201_CREATED)
async def create_handoff(
    call_id: uuid.UUID,
    payload: HandoffRequest,
    context: RequireOperator,
    session: DbSession,
) -> HandoffOut:
    """Hand the conversation to WhatsApp, carrying the transcript summary over."""
    call = await call_service.get_call(session, context.business_id, call_id)
    handoff = await whatsapp.initiate_handoff(
        session,
        call,
        reason=payload.reason,
        summary=payload.summary,
        language=Language(call.language) if call.language else Language.HINDI,
        to_number=payload.to_number,
    )
    return HandoffOut.model_validate(handoff)


@router.get("/handoffs", response_model=Page[HandoffOut])
async def list_handoffs(
    context: CurrentContext,
    session: DbSession,
    call_id: Annotated[uuid.UUID | None, Query()] = None,
    status_filter: Annotated[HandoffStatus | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[HandoffOut]:
    from sqlalchemy import func, select

    stmt = tenant_select(WhatsAppHandoff, context.business_id)
    if call_id is not None:
        stmt = stmt.where(WhatsAppHandoff.call_id == call_id)
    if status_filter is not None:
        stmt = stmt.where(WhatsAppHandoff.status == status_filter)

    total = await session.scalar(select(func.count()).select_from(stmt.order_by(None).subquery()))
    rows = (
        (
            await session.execute(
                stmt.order_by(WhatsAppHandoff.created_at.desc()).limit(limit).offset(offset)
            )
        )
        .scalars()
        .all()
    )
    return Page[HandoffOut](
        items=[HandoffOut.model_validate(h) for h in rows],
        total=int(total or 0),
        limit=limit,
        offset=offset,
    )


@router.post("/handoffs/{handoff_id}/retry", response_model=HandoffOut)
async def retry_handoff(
    handoff_id: uuid.UUID, context: RequireOperator, session: DbSession
) -> HandoffOut:
    """Re-send a handoff that the provider rejected."""
    handoff = await whatsapp.retry_handoff(session, context.business_id, handoff_id)
    return HandoffOut.model_validate(handoff)
