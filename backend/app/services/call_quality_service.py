"""Call quality monitoring: latency, jitter and packet loss samples.

The media edge posts a sample every few seconds while a call is live. Each
sample is graded independently and, when it crosses into ``poor``, an event is
emitted so the live dashboard can flag the call without polling. Rows are
append-only — the summary a caller sees is computed from the raw samples at
read time rather than maintained as a running aggregate, so there is nothing
to keep in sync.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.events import EventType, emit
from app.core.logging import get_logger
from app.core.tenancy import get_owned_or_404, tenant_select
from app.models.call import CallLog, CallQualityMetric
from app.models.enums import QualityGrade

logger = get_logger(__name__)

#: Above these, a sample is graded ``poor`` regardless of the others.
POOR_LATENCY_MS = 400
POOR_JITTER_MS = 60.0
POOR_PACKET_LOSS_PCT = 5.0

#: Above these (but under the ``poor`` thresholds), a sample is ``fair``.
FAIR_LATENCY_MS = 200
FAIR_JITTER_MS = 30.0
FAIR_PACKET_LOSS_PCT = 1.5

#: Samples fetched for a call's detail view.
SAMPLE_LIST_LIMIT = 500


def grade_sample(*, latency_ms: int, jitter_ms: float, packet_loss_pct: float) -> QualityGrade:
    """The worst of the three dimensions decides the sample's grade."""
    if (
        latency_ms >= POOR_LATENCY_MS
        or jitter_ms >= POOR_JITTER_MS
        or packet_loss_pct >= POOR_PACKET_LOSS_PCT
    ):
        return QualityGrade.POOR
    if (
        latency_ms >= FAIR_LATENCY_MS
        or jitter_ms >= FAIR_JITTER_MS
        or packet_loss_pct >= FAIR_PACKET_LOSS_PCT
    ):
        return QualityGrade.FAIR
    return QualityGrade.GOOD


async def record_sample(
    session: AsyncSession,
    business_id: uuid.UUID,
    call_id: uuid.UUID,
    *,
    latency_ms: int,
    jitter_ms: float,
    packet_loss_pct: float,
    mos_score: float | None = None,
    source: str = "media_edge",
    sampled_at: datetime | None = None,
) -> CallQualityMetric:
    """Grade and persist one sample, alerting watchers if it is poor."""
    call = await get_owned_or_404(session, CallLog, call_id, business_id, label="Call")
    grade = grade_sample(
        latency_ms=latency_ms, jitter_ms=jitter_ms, packet_loss_pct=packet_loss_pct
    )

    sample = CallQualityMetric(
        business_id=business_id,
        call_id=call_id,
        sampled_at=sampled_at or datetime.now(UTC),
        latency_ms=latency_ms,
        jitter_ms=jitter_ms,
        packet_loss_pct=packet_loss_pct,
        mos_score=mos_score,
        grade=grade,
        source=source,
    )
    session.add(sample)
    await session.flush()

    event_type = (
        EventType.QUALITY_DEGRADED if grade == QualityGrade.POOR else EventType.QUALITY_SAMPLE
    )
    await emit(
        event_type,
        business_id,
        call_id=call_id,
        latency_ms=latency_ms,
        jitter_ms=jitter_ms,
        packet_loss_pct=packet_loss_pct,
        mos_score=mos_score,
        grade=grade.value,
    )
    if grade == QualityGrade.POOR:
        logger.warning(
            "Poor call quality on %s (latency=%sms jitter=%sms loss=%s%%)",
            call.id,
            latency_ms,
            jitter_ms,
            packet_loss_pct,
        )
    return sample


async def list_samples(
    session: AsyncSession,
    business_id: uuid.UUID,
    call_id: uuid.UUID,
    *,
    limit: int = SAMPLE_LIST_LIMIT,
) -> list[CallQualityMetric]:
    await get_owned_or_404(session, CallLog, call_id, business_id, label="Call")
    result = await session.execute(
        tenant_select(CallQualityMetric, business_id)
        .where(CallQualityMetric.call_id == call_id)
        .order_by(CallQualityMetric.sampled_at)
        .limit(limit)
    )
    return list(result.scalars().all())


_GRADE_RANK = {QualityGrade.GOOD: 0, QualityGrade.FAIR: 1, QualityGrade.POOR: 2}


@dataclass(slots=True)
class QualitySummary:
    call_id: uuid.UUID
    sample_count: int
    avg_latency_ms: float | None
    max_latency_ms: int | None
    avg_jitter_ms: float | None
    max_jitter_ms: float | None
    avg_packet_loss_pct: float | None
    max_packet_loss_pct: float | None
    worst_grade: QualityGrade | None


async def get_summary(
    session: AsyncSession, business_id: uuid.UUID, call_id: uuid.UUID
) -> QualitySummary:
    """Aggregate every sample recorded for a call so far."""
    await get_owned_or_404(session, CallLog, call_id, business_id, label="Call")

    row = (
        await session.execute(
            select(
                func.count(CallQualityMetric.id),
                func.avg(CallQualityMetric.latency_ms),
                func.max(CallQualityMetric.latency_ms),
                func.avg(CallQualityMetric.jitter_ms),
                func.max(CallQualityMetric.jitter_ms),
                func.avg(CallQualityMetric.packet_loss_pct),
                func.max(CallQualityMetric.packet_loss_pct),
            ).where(
                CallQualityMetric.call_id == call_id,
                CallQualityMetric.business_id == business_id,
            )
        )
    ).one()
    count = int(row[0] or 0)

    worst_grade: QualityGrade | None = None
    if count:
        grades = (
            await session.execute(
                select(CallQualityMetric.grade).where(
                    CallQualityMetric.call_id == call_id,
                    CallQualityMetric.business_id == business_id,
                )
            )
        ).scalars().all()
        worst_grade = max((QualityGrade(g) for g in grades), key=lambda g: _GRADE_RANK[g])

    return QualitySummary(
        call_id=call_id,
        sample_count=count,
        avg_latency_ms=float(row[1]) if row[1] is not None else None,
        max_latency_ms=int(row[2]) if row[2] is not None else None,
        avg_jitter_ms=float(row[3]) if row[3] is not None else None,
        max_jitter_ms=float(row[4]) if row[4] is not None else None,
        avg_packet_loss_pct=float(row[5]) if row[5] is not None else None,
        max_packet_loss_pct=float(row[6]) if row[6] is not None else None,
        worst_grade=worst_grade,
    )
