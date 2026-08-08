"""``/api/v1/phone-numbers`` — telephony number inventory."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Body, Query, status

from app.core.deps import CurrentContext, DbSession, RequireAdmin
from app.models.enums import PhoneNumberStatus
from app.schemas.call import PhoneNumberOut, PhoneNumberUpdate, ProvisionNumberRequest
from app.schemas.common import Page
from app.services import phone_number_service

router = APIRouter(prefix="/phone-numbers", tags=["phone-numbers"])


@router.post("/provision", response_model=PhoneNumberOut, status_code=status.HTTP_201_CREATED)
async def provision(
    payload: ProvisionNumberRequest, context: RequireAdmin, session: DbSession
) -> PhoneNumberOut:
    number = await phone_number_service.provision_number(session, context.business_id, payload)
    return PhoneNumberOut.model_validate(number)


@router.get("", response_model=Page[PhoneNumberOut])
async def list_numbers(
    context: CurrentContext,
    session: DbSession,
    status_filter: Annotated[PhoneNumberStatus | None, Query(alias="status")] = None,
    agent_id: Annotated[uuid.UUID | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[PhoneNumberOut]:
    numbers, total = await phone_number_service.list_numbers(
        session,
        context.business_id,
        status=status_filter,
        agent_id=agent_id,
        limit=limit,
        offset=offset,
    )
    return Page[PhoneNumberOut](
        items=[PhoneNumberOut.model_validate(n) for n in numbers],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/{number_id}", response_model=PhoneNumberOut)
async def get_number(
    number_id: uuid.UUID, context: CurrentContext, session: DbSession
) -> PhoneNumberOut:
    number = await phone_number_service.get_number(session, context.business_id, number_id)
    return PhoneNumberOut.model_validate(number)


@router.patch("/{number_id}", response_model=PhoneNumberOut)
async def update_number(
    number_id: uuid.UUID,
    payload: PhoneNumberUpdate,
    context: RequireAdmin,
    session: DbSession,
) -> PhoneNumberOut:
    number = await phone_number_service.update_number(
        session, context.business_id, number_id, payload
    )
    return PhoneNumberOut.model_validate(number)


@router.post("/{number_id}/assign", response_model=PhoneNumberOut)
async def assign_agent(
    number_id: uuid.UUID,
    context: RequireAdmin,
    session: DbSession,
    agent_id: Annotated[uuid.UUID, Body(embed=True)],
) -> PhoneNumberOut:
    """Route inbound calls on this line to the given agent."""
    number = await phone_number_service.assign_agent(
        session, context.business_id, number_id, agent_id
    )
    return PhoneNumberOut.model_validate(number)


@router.delete("/{number_id}", response_model=PhoneNumberOut)
async def release_number(
    number_id: uuid.UUID, context: RequireAdmin, session: DbSession
) -> PhoneNumberOut:
    """Release the number back to the provider and soft delete the record."""
    number = await phone_number_service.release_number(session, context.business_id, number_id)
    return PhoneNumberOut.model_validate(number)
