"""``/api/v1/leads`` — the qualification pipeline and its scoring rules."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Query, Response, status

from app.core.deps import CurrentContext, DbSession, RequireAdmin, RequireOperator
from app.models.enums import LeadStatus, LeadTier
from app.schemas.common import Page, normalize_phone
from app.schemas.lead import (
    LeadCreate,
    LeadDetailOut,
    LeadOut,
    LeadUpdate,
    PipelineSummaryOut,
    QualificationConfigOut,
    QualificationWeightsUpdate,
)
from app.services import lead_service
from app.services.bant import QualificationConfig

router = APIRouter(prefix="/leads", tags=["leads"])


def _config_out(config: QualificationConfig) -> QualificationConfigOut:
    return QualificationConfigOut(
        weights={dimension.value: weight for dimension, weight in config.weights.items()},
        hot_at=config.hot_at,
        warm_at=config.warm_at,
        qualify_at=config.qualify_at,
        currency_floor=config.currency_floor,
    )


# Static paths come before ``/{lead_id}`` so "summary" and "config" are not
# parsed as UUIDs.
@router.get("/summary", response_model=PipelineSummaryOut)
async def get_pipeline_summary(context: CurrentContext, session: DbSession) -> PipelineSummaryOut:
    """Counts, tier mix and average score for the pipeline header."""
    summary = await lead_service.pipeline_summary(session, context.business_id)
    return PipelineSummaryOut(**summary)


@router.get("/config", response_model=QualificationConfigOut)
async def get_qualification_config(
    context: CurrentContext, session: DbSession
) -> QualificationConfigOut:
    return _config_out(await lead_service.get_config(session, context.business_id))


@router.put("/config", response_model=QualificationConfigOut)
async def update_qualification_config(
    payload: QualificationWeightsUpdate, context: RequireAdmin, session: DbSession
) -> QualificationConfigOut:
    """Change the weights or thresholds.

    Leads already scored keep their numbers until they are explicitly rescored,
    so a weight change never rewrites a salesperson's pipeline underneath them.
    """
    config = await lead_service.update_config(session, context.business_id, payload)
    return _config_out(config)


@router.post("", response_model=LeadDetailOut, status_code=status.HTTP_201_CREATED)
async def create_lead(
    payload: LeadCreate, context: RequireOperator, session: DbSession
) -> LeadDetailOut:
    lead = await lead_service.capture(session, context.business_id, payload)
    return _detail(lead)


@router.get("", response_model=Page[LeadOut])
async def list_leads(
    context: CurrentContext,
    session: DbSession,
    status_filter: Annotated[LeadStatus | None, Query(alias="status")] = None,
    tier: Annotated[LeadTier | None, Query()] = None,
    agent_id: Annotated[uuid.UUID | None, Query()] = None,
    min_score: Annotated[int | None, Query(ge=0, le=100)] = None,
    contact_phone: Annotated[str | None, Query(max_length=20)] = None,
    date_from: Annotated[datetime | None, Query()] = None,
    date_to: Annotated[datetime | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[LeadOut]:
    """The pipeline, highest score first."""
    leads, total = await lead_service.list_leads(
        session,
        context.business_id,
        status=status_filter,
        tier=tier,
        agent_id=agent_id,
        min_score=min_score,
        contact_phone=normalize_phone(contact_phone) if contact_phone else None,
        date_from=date_from,
        date_to=date_to,
        limit=limit,
        offset=offset,
    )
    return Page[LeadOut](
        items=[LeadOut.model_validate(lead) for lead in leads],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/{lead_id}", response_model=LeadDetailOut)
async def get_lead(
    lead_id: uuid.UUID, context: CurrentContext, session: DbSession
) -> LeadDetailOut:
    """One lead, with the score broken out per BANT dimension."""
    return _detail(await lead_service.get(session, context.business_id, lead_id))


@router.patch("/{lead_id}", response_model=LeadDetailOut)
async def update_lead(
    lead_id: uuid.UUID,
    payload: LeadUpdate,
    context: RequireOperator,
    session: DbSession,
) -> LeadDetailOut:
    """Edit contact details, move the lead along, or correct a captured answer.

    Correcting an answer rescores the lead — that is the point of storing what
    the caller said rather than only the number it produced.
    """
    lead = await lead_service.update(session, context.business_id, lead_id, payload)
    return _detail(lead)


@router.post("/{lead_id}/rescore", response_model=LeadDetailOut)
async def rescore_lead(
    lead_id: uuid.UUID, context: RequireOperator, session: DbSession
) -> LeadDetailOut:
    """Recompute the score from the stored answers under the current weights."""
    lead = await lead_service.rescore(session, context.business_id, lead_id)
    return _detail(lead)


@router.delete("/{lead_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_lead(lead_id: uuid.UUID, context: RequireAdmin, session: DbSession) -> Response:
    """Soft delete — the row is retained with ``deleted_at`` set."""
    await lead_service.soft_delete(session, context.business_id, lead_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _detail(lead) -> LeadDetailOut:
    detail = LeadDetailOut.model_validate(lead)
    detail.breakdown = lead_service.breakdown(lead)
    return detail
