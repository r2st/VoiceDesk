"""Agent availability and inbound call routing.

An agent is "available" when it is active and holds fewer live calls than its
configured concurrency limit. Inbound routing prefers the agent bound to the
dialled number, but falls back to another available agent in the business
rather than ringing straight through to an agent that is paused or already at
capacity — the caller gets an answer, and if nothing is available, the caller
is a voicemail candidate instead.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tenancy import tenant_select
from app.models.call import CallLog
from app.models.enums import AgentStatus, CallStatus
from app.models.voice_agent import VoiceAgent

#: Calls that occupy one of an agent's concurrency slots.
LIVE_STATUSES = (CallStatus.QUEUED, CallStatus.RINGING, CallStatus.IN_PROGRESS)


@dataclass(slots=True)
class AgentAvailability:
    agent: VoiceAgent
    active_calls: int

    @property
    def is_available(self) -> bool:
        return self.agent.status == AgentStatus.ACTIVE and self.reason is None

    @property
    def reason(self) -> str | None:
        if self.agent.status != AgentStatus.ACTIVE:
            return f"agent_{self.agent.status}"
        if self.active_calls >= self.agent.max_concurrent_calls:
            return "at_capacity"
        return None


async def _active_call_counts(
    session: AsyncSession, business_id: uuid.UUID, agent_ids: list[uuid.UUID]
) -> dict[uuid.UUID, int]:
    if not agent_ids:
        return {}
    result = await session.execute(
        select(CallLog.agent_id, func.count())
        .where(
            CallLog.business_id == business_id,
            CallLog.agent_id.in_(agent_ids),
            CallLog.status.in_([s.value for s in LIVE_STATUSES]),
            CallLog.deleted_at.is_(None),
        )
        .group_by(CallLog.agent_id)
    )
    return {row[0]: int(row[1]) for row in result.all()}


async def get_agent_availability(
    session: AsyncSession, business_id: uuid.UUID, agent: VoiceAgent
) -> AgentAvailability:
    counts = await _active_call_counts(session, business_id, [agent.id])
    return AgentAvailability(agent=agent, active_calls=counts.get(agent.id, 0))


async def list_agent_availability(
    session: AsyncSession, business_id: uuid.UUID
) -> list[AgentAvailability]:
    """Availability for every non-deleted agent, for the routing dashboard."""
    result = await session.execute(
        tenant_select(VoiceAgent, business_id).order_by(VoiceAgent.name)
    )
    agents = list(result.scalars().all())
    counts = await _active_call_counts(session, business_id, [a.id for a in agents])
    return [AgentAvailability(agent=a, active_calls=counts.get(a.id, 0)) for a in agents]


async def select_available_agent(
    session: AsyncSession,
    business_id: uuid.UUID,
    *,
    preferred_agent_id: uuid.UUID | None = None,
) -> VoiceAgent | None:
    """Pick an agent to take an inbound call.

    Prefers ``preferred_agent_id`` (typically the number's bound agent) when it
    is available; otherwise falls back to the least-loaded available agent in
    the business. Returns ``None`` when nobody can take the call — the caller
    is then a voicemail candidate.
    """
    result = await session.execute(
        tenant_select(VoiceAgent, business_id).where(VoiceAgent.status == AgentStatus.ACTIVE)
    )
    active_agents = list(result.scalars().all())
    if not active_agents:
        return None

    counts = await _active_call_counts(session, business_id, [a.id for a in active_agents])
    availabilities = [
        AgentAvailability(agent=a, active_calls=counts.get(a.id, 0)) for a in active_agents
    ]
    available = {a.agent.id: a for a in availabilities if a.is_available}
    if not available:
        return None

    if preferred_agent_id is not None and preferred_agent_id in available:
        return available[preferred_agent_id].agent

    # Least-loaded first so load spreads across the team rather than always
    # landing on whichever agent happens to sort first.
    best = min(available.values(), key=lambda a: a.active_calls)
    return best.agent
