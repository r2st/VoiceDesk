"""``/api/v1/intents`` — caller intent definitions."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Query, Response, status

from app.core.deps import CurrentContext, DbSession, RequireAdmin
from app.schemas.agent import IntentCreate, IntentOut, IntentUpdate
from app.services import agent_service

router = APIRouter(prefix="/intents", tags=["intents"])


@router.post("", response_model=IntentOut, status_code=status.HTTP_201_CREATED)
async def create_intent(
    payload: IntentCreate, context: RequireAdmin, session: DbSession
) -> IntentOut:
    intent = await agent_service.create_intent(session, context.business_id, payload)
    return IntentOut.model_validate(intent)


@router.get("", response_model=list[IntentOut])
async def list_intents(
    context: CurrentContext,
    session: DbSession,
    agent_id: Annotated[uuid.UUID | None, Query()] = None,
    active_only: Annotated[bool, Query()] = False,
) -> list[IntentOut]:
    intents = await agent_service.list_intents(
        session, context.business_id, agent_id=agent_id, active_only=active_only
    )
    return [IntentOut.model_validate(i) for i in intents]


@router.patch("/{intent_id}", response_model=IntentOut)
async def update_intent(
    intent_id: uuid.UUID, payload: IntentUpdate, context: RequireAdmin, session: DbSession
) -> IntentOut:
    intent = await agent_service.update_intent(session, context.business_id, intent_id, payload)
    return IntentOut.model_validate(intent)


@router.delete("/{intent_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_intent(
    intent_id: uuid.UUID, context: RequireAdmin, session: DbSession
) -> Response:
    await agent_service.soft_delete_intent(session, context.business_id, intent_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
