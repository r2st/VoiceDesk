"""Voice agent and intent business logic. Every query is tenant-scoped."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.tenancy import get_owned_or_404, tenant_select
from app.models.business import Business
from app.models.call import CallLog, PhoneNumber
from app.models.enums import AgentStatus, CallStatus, PlanTier
from app.models.voice_agent import Intent, VoiceAgent
from app.schemas.agent import (
    IntentCreate,
    IntentUpdate,
    VoiceAgentCreate,
    VoiceAgentUpdate,
)
from app.services.flow import default_flow, validate_flow
from app.services.plans import get_plan

DEFAULT_GREETINGS = {
    "hi": "नमस्ते! मैं आपकी किस प्रकार सहायता कर सकता हूँ?",
    "en": "Hello! How may I help you today?",
    "ta": "வணக்கம்! நான் உங்களுக்கு எப்படி உதவ முடியும்?",
    "te": "నమస్కారం! నేను మీకు ఎలా సహాయం చేయగలను?",
    "mr": "नमस्कार! मी आपली कशी मदत करू शकतो?",
    "bn": "নমস্কার! আমি আপনাকে কীভাবে সাহায্য করতে পারি?",
    "kn": "ನಮಸ್ಕಾರ! ನಾನು ನಿಮಗೆ ಹೇಗೆ ಸಹಾಯ ಮಾಡಬಹುದು?",
}


async def _enforce_plan_limits(
    session: AsyncSession, business_id: uuid.UUID, payload: VoiceAgentCreate
) -> None:
    """Agent count and language count are capped by the business plan (§6.1)."""
    business = await session.get(Business, business_id)
    if business is None or business.deleted_at is not None:
        raise NotFoundError("Business not found.")
    plan = get_plan(business.plan)

    if plan.max_agents is not None:
        count = await session.scalar(
            select(func.count())
            .select_from(VoiceAgent)
            .where(VoiceAgent.business_id == business_id, VoiceAgent.deleted_at.is_(None))
        )
        if (count or 0) >= plan.max_agents:
            raise ValidationError(
                f"The {plan.name} plan allows {plan.max_agents} agent(s). "
                "Upgrade the plan to add more.",
                details={"plan": plan.tier.value, "max_agents": plan.max_agents},
            )

    if plan.max_languages is not None:
        languages = {payload.language, *payload.supported_languages}
        if len(languages) > plan.max_languages:
            raise ValidationError(
                f"The {plan.name} plan allows {plan.max_languages} language(s).",
                details={"plan": plan.tier.value, "max_languages": plan.max_languages},
            )


async def create_agent(
    session: AsyncSession, business_id: uuid.UUID, payload: VoiceAgentCreate
) -> VoiceAgent:
    await _enforce_plan_limits(session, business_id, payload)

    duplicate = await session.execute(
        tenant_select(VoiceAgent, business_id).where(VoiceAgent.name == payload.name)
    )
    if duplicate.scalar_one_or_none() is not None:
        raise ConflictError(f"An agent named '{payload.name}' already exists.")

    greeting = payload.greeting or DEFAULT_GREETINGS.get(
        payload.language.value, DEFAULT_GREETINGS["en"]
    )
    flow_json = payload.flow_json or default_flow(greeting, payload.use_case.value)
    validate_flow(flow_json)

    agent = VoiceAgent(
        business_id=business_id,
        name=payload.name,
        description=payload.description,
        use_case=payload.use_case,
        status=payload.status,
        language=payload.language,
        supported_languages=[lang.value for lang in payload.supported_languages],
        voice_id=payload.voice_id,
        persona=payload.persona or _default_persona(payload),
        greeting=greeting,
        fallback_message=payload.fallback_message,
        flow_json=flow_json,
        flow_version=1,
        max_call_duration_sec=payload.max_call_duration_sec,
        max_concurrent_calls=payload.max_concurrent_calls,
        handoff_confidence_threshold=payload.handoff_confidence_threshold,
        whatsapp_handoff_enabled=payload.whatsapp_handoff_enabled,
        recording_enabled=payload.recording_enabled,
        voicemail_enabled=payload.voicemail_enabled,
    )
    session.add(agent)
    await session.flush()
    return agent


def _default_persona(payload: VoiceAgentCreate) -> str:
    return (
        f"You are {payload.name}, a polite and efficient voice assistant handling "
        f"{payload.use_case.value.replace('_', ' ')} calls for an Indian business. "
        "Keep replies short and conversational — one or two sentences — because they "
        "are spoken aloud over a phone line. Mirror the caller's language, including "
        "Hindi-English code-switching. Never invent facts about orders, payments or "
        "appointments; if you do not know, say so and offer to connect a human."
    )


async def list_agents(
    session: AsyncSession,
    business_id: uuid.UUID,
    *,
    status: AgentStatus | None = None,
    search: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[VoiceAgent], int]:
    stmt = tenant_select(VoiceAgent, business_id)
    if status is not None:
        stmt = stmt.where(VoiceAgent.status == status)
    if search:
        stmt = stmt.where(VoiceAgent.name.ilike(f"%{search}%"))

    total = await session.scalar(select(func.count()).select_from(stmt.order_by(None).subquery()))
    result = await session.execute(
        stmt.order_by(VoiceAgent.created_at.desc()).limit(limit).offset(offset)
    )
    return list(result.scalars().all()), int(total or 0)


async def get_agent(
    session: AsyncSession, business_id: uuid.UUID, agent_id: uuid.UUID
) -> VoiceAgent:
    return await get_owned_or_404(session, VoiceAgent, agent_id, business_id, label="Agent")


async def update_agent(
    session: AsyncSession,
    business_id: uuid.UUID,
    agent_id: uuid.UUID,
    payload: VoiceAgentUpdate,
) -> VoiceAgent:
    agent = await get_agent(session, business_id, agent_id)
    changes = payload.model_dump(exclude_unset=True)

    if "name" in changes and changes["name"] != agent.name:
        duplicate = await session.execute(
            tenant_select(VoiceAgent, business_id).where(
                VoiceAgent.name == changes["name"], VoiceAgent.id != agent_id
            )
        )
        if duplicate.scalar_one_or_none() is not None:
            raise ConflictError(f"An agent named '{changes['name']}' already exists.")

    if "supported_languages" in changes and changes["supported_languages"] is not None:
        changes["supported_languages"] = [
            lang.value if hasattr(lang, "value") else lang
            for lang in changes["supported_languages"]
        ]

    for field, value in changes.items():
        setattr(agent, field, value)
    await session.flush()
    return agent


async def update_flow(
    session: AsyncSession, business_id: uuid.UUID, agent_id: uuid.UUID, flow_json: dict
) -> VoiceAgent:
    agent = await get_agent(session, business_id, agent_id)
    validate_flow(flow_json)
    agent.flow_json = flow_json
    agent.flow_version = (agent.flow_version or 0) + 1
    await session.flush()
    return agent


async def soft_delete_agent(
    session: AsyncSession, business_id: uuid.UUID, agent_id: uuid.UUID
) -> None:
    """Soft delete. Refuses while calls are still live on this agent."""
    agent = await get_agent(session, business_id, agent_id)

    active_statuses = [CallStatus.QUEUED, CallStatus.RINGING, CallStatus.IN_PROGRESS]
    live = await session.scalar(
        select(func.count())
        .select_from(CallLog)
        .where(
            CallLog.business_id == business_id,
            CallLog.agent_id == agent_id,
            CallLog.status.in_(active_statuses),
            CallLog.deleted_at.is_(None),
        )
    )
    if live:
        raise ConflictError(
            f"{live} call(s) are still in progress on this agent.",
            details={"active_calls": int(live)},
        )

    now = datetime.now(UTC)
    agent.deleted_at = now
    agent.status = AgentStatus.PAUSED

    # Detach the agent from any numbers routing to it, so inbound calls do not
    # land on a deleted agent.
    numbers = await session.execute(
        tenant_select(PhoneNumber, business_id).where(PhoneNumber.agent_id == agent_id)
    )
    for number in numbers.scalars().all():
        number.agent_id = None

    await session.flush()


async def duplicate_agent(
    session: AsyncSession, business_id: uuid.UUID, agent_id: uuid.UUID, new_name: str | None = None
) -> VoiceAgent:
    """Clone an agent and its flow as a fresh draft."""
    source = await get_agent(session, business_id, agent_id)
    name = new_name or f"{source.name} (copy)"

    duplicate = await session.execute(
        tenant_select(VoiceAgent, business_id).where(VoiceAgent.name == name)
    )
    if duplicate.scalar_one_or_none() is not None:
        raise ConflictError(f"An agent named '{name}' already exists.")

    business = await session.get(Business, business_id)
    plan = get_plan(business.plan if business else PlanTier.STARTER)
    if plan.max_agents is not None:
        count = await session.scalar(
            select(func.count())
            .select_from(VoiceAgent)
            .where(VoiceAgent.business_id == business_id, VoiceAgent.deleted_at.is_(None))
        )
        if (count or 0) >= plan.max_agents:
            raise ValidationError(f"The {plan.name} plan allows {plan.max_agents} agent(s).")

    clone = VoiceAgent(
        business_id=business_id,
        name=name,
        description=source.description,
        use_case=source.use_case,
        status=AgentStatus.DRAFT,
        language=source.language,
        supported_languages=list(source.supported_languages or []),
        voice_id=source.voice_id,
        persona=source.persona,
        greeting=source.greeting,
        fallback_message=source.fallback_message,
        flow_json=dict(source.flow_json or {}),
        flow_version=1,
        max_call_duration_sec=source.max_call_duration_sec,
        max_concurrent_calls=source.max_concurrent_calls,
        handoff_confidence_threshold=source.handoff_confidence_threshold,
        whatsapp_handoff_enabled=source.whatsapp_handoff_enabled,
        recording_enabled=source.recording_enabled,
        voicemail_enabled=source.voicemail_enabled,
    )
    session.add(clone)
    await session.flush()
    return clone


# --------------------------------------------------------------------------- #
# Intents
# --------------------------------------------------------------------------- #
async def create_intent(
    session: AsyncSession, business_id: uuid.UUID, payload: IntentCreate
) -> Intent:
    if payload.agent_id is not None:
        await get_agent(session, business_id, payload.agent_id)

    duplicate = await session.execute(
        tenant_select(Intent, business_id).where(
            Intent.name == payload.name, Intent.agent_id == payload.agent_id
        )
    )
    if duplicate.scalar_one_or_none() is not None:
        raise ConflictError(f"An intent named '{payload.name}' already exists in this scope.")

    intent = Intent(
        business_id=business_id,
        agent_id=payload.agent_id,
        name=payload.name,
        description=payload.description,
        sample_phrases=payload.sample_phrases,
        action_type=payload.action_type,
        parameters_json=payload.parameters_json,
        is_active=payload.is_active,
        priority=payload.priority,
    )
    session.add(intent)
    await session.flush()
    return intent


async def list_intents(
    session: AsyncSession,
    business_id: uuid.UUID,
    *,
    agent_id: uuid.UUID | None = None,
    include_global: bool = True,
    active_only: bool = False,
) -> list[Intent]:
    """Intents visible to an agent: its own plus business-wide ones."""
    stmt = tenant_select(Intent, business_id)
    if agent_id is not None:
        stmt = (
            stmt.where((Intent.agent_id == agent_id) | (Intent.agent_id.is_(None)))
            if include_global
            else stmt.where(Intent.agent_id == agent_id)
        )
    if active_only:
        stmt = stmt.where(Intent.is_active.is_(True))
    result = await session.execute(stmt.order_by(Intent.priority, Intent.name))
    return list(result.scalars().all())


async def update_intent(
    session: AsyncSession, business_id: uuid.UUID, intent_id: uuid.UUID, payload: IntentUpdate
) -> Intent:
    intent = await get_owned_or_404(session, Intent, intent_id, business_id, label="Intent")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(intent, field, value)
    await session.flush()
    return intent


async def soft_delete_intent(
    session: AsyncSession, business_id: uuid.UUID, intent_id: uuid.UUID
) -> None:
    intent = await get_owned_or_404(session, Intent, intent_id, business_id, label="Intent")
    intent.deleted_at = datetime.now(UTC)
    intent.is_active = False
    await session.flush()
