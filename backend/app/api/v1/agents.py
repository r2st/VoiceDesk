"""``/api/v1/agents`` — voice agent CRUD and conversation flow management."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Body, Query, Response, status

from app.core.deps import CurrentContext, DbSession, RequireAdmin
from app.core.errors import ValidationError
from app.models.enums import AgentStatus
from app.schemas.agent import (
    FlowUpdate,
    FlowValidationResult,
    VoiceAgentCreate,
    VoiceAgentOut,
    VoiceAgentUpdate,
)
from app.schemas.common import Page
from app.services import agent_service
from app.services.flow import validate_flow

router = APIRouter(prefix="/agents", tags=["agents"])


@router.post("", response_model=VoiceAgentOut, status_code=status.HTTP_201_CREATED)
async def create_agent(
    payload: VoiceAgentCreate, context: RequireAdmin, session: DbSession
) -> VoiceAgentOut:
    agent = await agent_service.create_agent(session, context.business_id, payload)
    return VoiceAgentOut.model_validate(agent)


@router.get("", response_model=Page[VoiceAgentOut])
async def list_agents(
    context: CurrentContext,
    session: DbSession,
    status_filter: Annotated[AgentStatus | None, Query(alias="status")] = None,
    search: Annotated[str | None, Query(max_length=120)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[VoiceAgentOut]:
    agents, total = await agent_service.list_agents(
        session,
        context.business_id,
        status=status_filter,
        search=search,
        limit=limit,
        offset=offset,
    )
    return Page[VoiceAgentOut](
        items=[VoiceAgentOut.model_validate(a) for a in agents],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/{agent_id}", response_model=VoiceAgentOut)
async def get_agent(
    agent_id: uuid.UUID, context: CurrentContext, session: DbSession
) -> VoiceAgentOut:
    agent = await agent_service.get_agent(session, context.business_id, agent_id)
    return VoiceAgentOut.model_validate(agent)


@router.patch("/{agent_id}", response_model=VoiceAgentOut)
async def update_agent(
    agent_id: uuid.UUID,
    payload: VoiceAgentUpdate,
    context: RequireAdmin,
    session: DbSession,
) -> VoiceAgentOut:
    agent = await agent_service.update_agent(session, context.business_id, agent_id, payload)
    return VoiceAgentOut.model_validate(agent)


@router.put("/{agent_id}/flow", response_model=VoiceAgentOut)
async def update_flow(
    agent_id: uuid.UUID, payload: FlowUpdate, context: RequireAdmin, session: DbSession
) -> VoiceAgentOut:
    agent = await agent_service.update_flow(
        session, context.business_id, agent_id, payload.flow_json
    )
    return VoiceAgentOut.model_validate(agent)


@router.post("/validate-flow", response_model=FlowValidationResult)
async def validate_flow_endpoint(
    payload: FlowUpdate, context: CurrentContext
) -> FlowValidationResult:
    """Dry-run flow validation for the agent builder — never touches the database."""
    try:
        flow = validate_flow(payload.flow_json)
    except ValidationError as exc:
        return FlowValidationResult(valid=False, errors=exc.details.get("errors", []))
    if flow is None:
        return FlowValidationResult(
            valid=False, errors=[{"location": "flow_json", "message": "Flow is empty."}]
        )
    return FlowValidationResult(
        valid=True,
        node_count=len(flow.nodes),
        terminal_nodes=sorted(flow.terminal_node_ids()),
    )


@router.post("/{agent_id}/duplicate", response_model=VoiceAgentOut, status_code=201)
async def duplicate_agent(
    agent_id: uuid.UUID,
    context: RequireAdmin,
    session: DbSession,
    name: Annotated[str | None, Body(embed=True)] = None,
) -> VoiceAgentOut:
    clone = await agent_service.duplicate_agent(session, context.business_id, agent_id, name)
    return VoiceAgentOut.model_validate(clone)


@router.delete("/{agent_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_agent(agent_id: uuid.UUID, context: RequireAdmin, session: DbSession) -> Response:
    """Soft delete — the row is retained with ``deleted_at`` set."""
    await agent_service.soft_delete_agent(session, context.business_id, agent_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
