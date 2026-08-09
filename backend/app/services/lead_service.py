"""Lead capture, scoring and CRM handoff (design doc §4.9, §4.6).

Leads arrive from three places — the voice agent mid-call, dashboard staff, and
the public API — and all three land here so that a lead is scored exactly one
way regardless of who created it.

Scoring itself lives in :mod:`app.services.bant` as a pure function. This module
is the part that touches the database: it stores the caller's answers, records
the score derived from them, and re-derives that score whenever either changes.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError, ValidationError
from app.core.logging import get_logger, mask_phone
from app.core.tenancy import get_owned_or_404, tenant_select
from app.models.business import Business
from app.models.enums import BANTDimension, CrmPushStatus, LeadSource, LeadStatus, LeadTier
from app.models.lead import Lead
from app.schemas.lead import (
    BantAnswersIn,
    DimensionScoreOut,
    LeadCreate,
    LeadUpdate,
    QualificationWeightsUpdate,
)
from app.services import bant
from app.services.bant import BantAnswers, QualificationConfig

logger = get_logger(__name__)

#: Statuses staff set by hand. ``QUALIFIED``/``DISQUALIFIED`` are outcomes of
#: scoring, not choices — a salesperson marking a 12/100 lead "qualified" would
#: make the score meaningless — so they are not settable through the API.
STAFF_SETTABLE_STATUSES = frozenset({LeadStatus.CONTACTED, LeadStatus.CONVERTED, LeadStatus.LOST})

#: Statuses scoring is still free to overwrite. Anything else means a person
#: has picked the lead up, and their judgement outranks the score from then on.
SCORING_OWNED_STATUSES = frozenset(
    {None, LeadStatus.NEW, LeadStatus.QUALIFIED, LeadStatus.DISQUALIFIED}
)


async def get_config(session: AsyncSession, business_id: uuid.UUID) -> QualificationConfig:
    """This tenant's scoring rules, or the platform defaults."""
    business = await session.get(Business, business_id)
    if business is None:
        raise NotFoundError("Business not found.")
    return bant.load_qualification_config(business.settings_json)


async def update_config(
    session: AsyncSession, business_id: uuid.UUID, payload: QualificationWeightsUpdate
) -> QualificationConfig:
    """Change the weights or thresholds. Existing leads keep their scores.

    Re-scoring the whole pipeline on a weight change would silently rewrite
    history under a salesperson mid-conversation; :func:`rescore` is the
    explicit way to do it.
    """
    business = await session.get(Business, business_id)
    if business is None:
        raise NotFoundError("Business not found.")

    settings = dict(business.settings_json or {})
    current = dict(settings.get("lead_qualification") or {})
    weights = dict(current.get("weights") or {})

    for dimension in ("budget", "authority", "need", "timeline"):
        value = getattr(payload, dimension)
        if value is not None:
            weights[dimension] = value
    if weights:
        current["weights"] = weights

    for threshold in ("hot_at", "warm_at", "qualify_at", "currency_floor"):
        value = getattr(payload, threshold)
        if value is not None:
            current[threshold] = value

    settings["lead_qualification"] = current
    business.settings_json = settings
    await session.flush()
    return bant.load_qualification_config(settings)


# --------------------------------------------------------------------------- #
# Creating and scoring
# --------------------------------------------------------------------------- #
async def capture(
    session: AsyncSession,
    business_id: uuid.UUID,
    payload: LeadCreate,
    *,
    config: QualificationConfig | None = None,
) -> Lead:
    """Create a lead and score it from whatever answers came with it."""
    resolved = config or await get_config(session, business_id)

    lead = Lead(
        business_id=business_id,
        agent_id=payload.agent_id,
        call_id=payload.call_id,
        contact_name=payload.contact_name,
        contact_phone=payload.contact_phone,
        contact_email=payload.contact_email,
        company=payload.company,
        interest=payload.interest,
        source=payload.source,
        language=payload.language,
        notes=payload.notes,
    )
    _apply_answers(lead, payload.answers)
    _apply_score(lead, resolved)

    session.add(lead)
    await session.flush()
    logger.info(
        "Captured lead %s (%s) scored %s/%s",
        lead.id,
        mask_phone(lead.contact_phone),
        lead.score,
        lead.tier,
    )
    return lead


async def capture_from_call(
    session: AsyncSession,
    business_id: uuid.UUID,
    *,
    call_id: uuid.UUID,
    agent_id: uuid.UUID | None,
    contact_name: str,
    contact_phone: str,
    answers: BantAnswers,
    interest: str | None = None,
    company: str | None = None,
    language: str | None = None,
    config: QualificationConfig | None = None,
) -> Lead:
    """Capture a lead from a qualification call, or update the one already on it.

    A call can reach the qualification node more than once — the caller revises
    an answer, or the flow loops back for a missing one — and each pass should
    sharpen the same lead rather than leave the pipeline with duplicates of one
    prospect.
    """
    resolved = config or await get_config(session, business_id)
    existing = (
        await session.execute(tenant_select(Lead, business_id).where(Lead.call_id == call_id))
    ).scalar_one_or_none()

    if existing is not None:
        # Only overwrite dimensions this pass actually captured; a second pass
        # that asked about timeline must not blank out the budget answer.
        _apply_answers(existing, answers, keep_missing=True)
        if interest:
            existing.interest = interest
        if company:
            existing.company = company
        _apply_score(existing, resolved)
        await session.flush()
        return existing

    lead = Lead(
        business_id=business_id,
        agent_id=agent_id,
        call_id=call_id,
        contact_name=contact_name,
        contact_phone=contact_phone,
        company=company,
        interest=interest,
        source=LeadSource.VOICE_CALL,
        language=language,
    )
    _apply_answers(lead, answers)
    _apply_score(lead, resolved)
    session.add(lead)
    await session.flush()
    logger.info("Call %s produced lead %s scored %s", call_id, lead.id, lead.score)
    return lead


async def rescore(session: AsyncSession, business_id: uuid.UUID, lead_id: uuid.UUID) -> Lead:
    """Recompute a lead's score from its stored answers and current weights."""
    lead = await get(session, business_id, lead_id)
    _apply_score(lead, await get_config(session, business_id))
    await session.flush()
    return lead


def _apply_answers(
    lead: Lead, answers: BantAnswers | BantAnswersIn, *, keep_missing: bool = False
) -> None:
    """Copy captured answers onto the row.

    ``keep_missing`` leaves a dimension untouched when this pass has nothing for
    it, which is what a second visit to the qualification node wants.
    """
    for dimension in ("budget", "authority", "need", "timeline"):
        value = getattr(answers, dimension, None)
        if value is None and keep_missing:
            continue
        setattr(lead, f"{dimension}_answer", value)


def _apply_score(lead: Lead, config: QualificationConfig) -> None:
    """Derive score, per-dimension scores, tier and status from the answers."""
    result = bant.qualify(
        BantAnswers(
            budget=lead.budget_answer,
            authority=lead.authority_answer,
            need=lead.need_answer,
            timeline=lead.timeline_answer,
        ),
        config,
    )

    lead.score = result.score
    lead.tier = result.tier
    lead.rationale = result.rationale()
    for dimension, scored in result.dimensions.items():
        setattr(lead, f"{dimension.value}_score", round(scored.score, 3))

    # A salesperson who has already picked the lead up owns its status from
    # then on; re-scoring must not drag a converted deal back to "qualified".
    # ``None`` is in the set because a lead scored before its first flush has
    # not had the column default applied yet — scoring still owns it.
    if lead.status in SCORING_OWNED_STATUSES:
        lead.status = LeadStatus.QUALIFIED if result.qualified else LeadStatus.DISQUALIFIED
        lead.qualified_at = datetime.now(UTC) if result.qualified else None

    # A lead that has just crossed the threshold becomes CRM work again — the
    # earlier pass may have parked it as unpushable when it did not qualify.
    if result.qualified and lead.crm_status != CrmPushStatus.SENT:
        lead.crm_status = CrmPushStatus.PENDING


# --------------------------------------------------------------------------- #
# Reading and editing
# --------------------------------------------------------------------------- #
async def get(session: AsyncSession, business_id: uuid.UUID, lead_id: uuid.UUID) -> Lead:
    return await get_owned_or_404(session, Lead, lead_id, business_id, label="Lead")


async def update(
    session: AsyncSession, business_id: uuid.UUID, lead_id: uuid.UUID, payload: LeadUpdate
) -> Lead:
    lead = await get(session, business_id, lead_id)

    for field in ("contact_name", "contact_email", "company", "interest", "notes"):
        value = getattr(payload, field)
        if value is not None:
            setattr(lead, field, value)

    if payload.status is not None:
        if payload.status not in STAFF_SETTABLE_STATUSES:
            raise ValidationError(
                f"'{payload.status}' is decided by scoring, not set by hand.",
                details={"allowed": sorted(STAFF_SETTABLE_STATUSES)},
            )
        lead.status = payload.status

    if payload.answers is not None:
        _apply_answers(lead, payload.answers, keep_missing=True)
        _apply_score(lead, await get_config(session, business_id))

    await session.flush()
    return lead


async def list_leads(
    session: AsyncSession,
    business_id: uuid.UUID,
    *,
    status: LeadStatus | None = None,
    tier: LeadTier | None = None,
    agent_id: uuid.UUID | None = None,
    min_score: int | None = None,
    contact_phone: str | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[Lead], int]:
    stmt = tenant_select(Lead, business_id)
    if status is not None:
        stmt = stmt.where(Lead.status == status)
    if tier is not None:
        stmt = stmt.where(Lead.tier == tier)
    if agent_id is not None:
        stmt = stmt.where(Lead.agent_id == agent_id)
    if min_score is not None:
        stmt = stmt.where(Lead.score >= min_score)
    if contact_phone:
        stmt = stmt.where(Lead.contact_phone.contains(contact_phone))
    if date_from is not None:
        stmt = stmt.where(Lead.created_at >= date_from)
    if date_to is not None:
        stmt = stmt.where(Lead.created_at < date_to)

    total = await session.scalar(select(func.count()).select_from(stmt.order_by(None).subquery()))
    result = await session.execute(
        # Highest score first: the pipeline is a work queue, not a log.
        stmt.order_by(Lead.score.desc(), Lead.created_at.desc()).limit(limit).offset(offset)
    )
    return list(result.scalars().all()), int(total or 0)


async def pipeline_summary(session: AsyncSession, business_id: uuid.UUID) -> dict:
    """Counts and averages for the pipeline header."""
    rows = (
        await session.execute(
            select(Lead.tier, Lead.status, Lead.score).where(
                Lead.business_id == business_id, Lead.deleted_at.is_(None)
            )
        )
    ).all()

    by_tier: dict[str, int] = {tier.value: 0 for tier in LeadTier}
    by_status: dict[str, int] = {status.value: 0 for status in LeadStatus}
    for tier, status, _ in rows:
        by_tier[tier] = by_tier.get(tier, 0) + 1
        by_status[status] = by_status.get(status, 0) + 1

    total = len(rows)
    qualified = sum(1 for _, status, _ in rows if status == LeadStatus.QUALIFIED)
    average = sum(score for _, _, score in rows) / total if total else 0.0

    return {
        "total": total,
        "by_tier": by_tier,
        "by_status": by_status,
        "average_score": round(average, 1),
        "qualified_rate": round(qualified / total, 3) if total else 0.0,
    }


async def due_for_crm_push(
    session: AsyncSession, *, limit: int = 200, max_attempts: int = 10
) -> list[Lead]:
    """Qualified leads still owed to a CRM, across every tenant.

    Failed pushes are included so a CRM that was down recovers on its own, up
    to ``max_attempts``. Oldest first — a lead that has been waiting is the one
    losing the most value.
    """
    result = await session.execute(
        select(Lead)
        .where(
            Lead.deleted_at.is_(None),
            Lead.status == LeadStatus.QUALIFIED,
            Lead.crm_status.in_((CrmPushStatus.PENDING, CrmPushStatus.FAILED)),
            Lead.crm_attempts < max_attempts,
        )
        .order_by(Lead.created_at)
        .limit(limit)
    )
    return list(result.scalars().all())


def breakdown(lead: Lead) -> list[DimensionScoreOut]:
    """The stored score, one row per dimension, for the lead detail view.

    Read off the row rather than recomputed, so what a salesperson sees is the
    scoring the lead actually carries — a weight change since capture shows up
    only once someone rescores.
    """
    return [
        DimensionScoreOut(
            dimension=dimension,
            answer=getattr(lead, f"{dimension.value}_answer"),
            score=getattr(lead, f"{dimension.value}_score"),
            percent=round(getattr(lead, f"{dimension.value}_score") * 100),
            reason=(lead.rationale or {}).get(dimension.value, "not scored"),
        )
        for dimension in BANTDimension
    ]


async def soft_delete(session: AsyncSession, business_id: uuid.UUID, lead_id: uuid.UUID) -> None:
    lead = await get(session, business_id, lead_id)
    lead.deleted_at = datetime.now(UTC)
    await session.flush()
