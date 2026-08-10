"""``/api/v1/calls/{call_id}/quality`` — call media-quality monitoring.

Latency, jitter and packet loss samples posted by the media edge while a call
is live, plus the rolled-up summary the transcript view and live board read.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, status

from app.core.deps import CurrentContext, DbSession, RequireOperator
from app.schemas.call import QualitySampleOut, QualitySampleRequest, QualitySummaryOut
from app.services import call_quality_service

router = APIRouter(prefix="/calls", tags=["call-quality"])


@router.post(
    "/{call_id}/quality",
    response_model=QualitySampleOut,
    status_code=status.HTTP_201_CREATED,
)
async def record_quality_sample(
    call_id: uuid.UUID,
    payload: QualitySampleRequest,
    context: RequireOperator,
    session: DbSession,
) -> QualitySampleOut:
    """Ingest one media-quality reading. Called by the telephony/media edge."""
    sample = await call_quality_service.record_sample(
        session,
        context.business_id,
        call_id,
        latency_ms=payload.latency_ms,
        jitter_ms=payload.jitter_ms,
        packet_loss_pct=payload.packet_loss_pct,
        mos_score=payload.mos_score,
        source=payload.source,
        sampled_at=payload.sampled_at,
    )
    return QualitySampleOut.model_validate(sample)


@router.get("/{call_id}/quality", response_model=list[QualitySampleOut])
async def list_quality_samples(
    call_id: uuid.UUID, context: CurrentContext, session: DbSession
) -> list[QualitySampleOut]:
    samples = await call_quality_service.list_samples(session, context.business_id, call_id)
    return [QualitySampleOut.model_validate(s) for s in samples]


@router.get("/{call_id}/quality/summary", response_model=QualitySummaryOut)
async def quality_summary(
    call_id: uuid.UUID, context: CurrentContext, session: DbSession
) -> QualitySummaryOut:
    summary = await call_quality_service.get_summary(session, context.business_id, call_id)
    return QualitySummaryOut(
        call_id=summary.call_id,
        sample_count=summary.sample_count,
        avg_latency_ms=summary.avg_latency_ms,
        max_latency_ms=summary.max_latency_ms,
        avg_jitter_ms=summary.avg_jitter_ms,
        max_jitter_ms=summary.max_jitter_ms,
        avg_packet_loss_pct=summary.avg_packet_loss_pct,
        max_packet_loss_pct=summary.max_packet_loss_pct,
        worst_grade=summary.worst_grade,
    )
