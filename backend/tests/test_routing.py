"""Agent availability and inbound call routing."""

from __future__ import annotations

from datetime import UTC, datetime

from app.models.call import CallLog
from app.models.enums import AgentStatus, CallDirection, CallStatus, TelephonyProvider
from app.services import call_service, routing_service


async def _live_call(session, business, agent, phone_number, n: int) -> CallLog:
    row = CallLog(
        business_id=business.id,
        agent_id=agent.id,
        phone_number_id=phone_number.id,
        direction=CallDirection.INBOUND,
        status=CallStatus.IN_PROGRESS,
        caller_number=f"+9199999{n:05d}",
        callee_number=phone_number.number,
        provider=TelephonyProvider.MOCK,
        provider_call_id=f"mock-live-{n}",
        started_at=datetime.now(UTC),
    )
    session.add(row)
    await session.flush()
    return row


class TestAgentAvailability:
    async def test_idle_agent_is_available(self, session, business, agent):
        availability = await routing_service.get_agent_availability(session, business.id, agent)
        assert availability.is_available is True
        assert availability.active_calls == 0
        assert availability.reason is None

    async def test_paused_agent_is_unavailable(self, session, business, agent):
        agent.status = AgentStatus.PAUSED
        await session.flush()

        availability = await routing_service.get_agent_availability(session, business.id, agent)
        assert availability.is_available is False
        assert availability.reason == "agent_paused"

    async def test_agent_at_capacity_is_unavailable(self, session, business, agent, phone_number):
        agent.max_concurrent_calls = 2
        await session.flush()
        await _live_call(session, business, agent, phone_number, 1)
        await _live_call(session, business, agent, phone_number, 2)

        availability = await routing_service.get_agent_availability(session, business.id, agent)
        assert availability.active_calls == 2
        assert availability.is_available is False
        assert availability.reason == "at_capacity"

    async def test_terminal_calls_do_not_count_towards_capacity(
        self, session, business, agent, phone_number
    ):
        live = await _live_call(session, business, agent, phone_number, 1)
        live.status = CallStatus.COMPLETED
        await session.flush()

        availability = await routing_service.get_agent_availability(session, business.id, agent)
        assert availability.active_calls == 0

    async def test_list_agent_availability_covers_every_agent(self, session, business, agent):
        availabilities = await routing_service.list_agent_availability(session, business.id)
        assert {a.agent.id for a in availabilities} == {agent.id}


class TestSelectAvailableAgent:
    async def test_prefers_the_requested_agent_when_available(self, session, business, agent):
        selected = await routing_service.select_available_agent(
            session, business.id, preferred_agent_id=agent.id
        )
        assert selected is not None and selected.id == agent.id

    async def test_falls_back_when_the_preferred_agent_is_paused(
        self, session, business, agent, second_agent
    ):
        agent.status = AgentStatus.PAUSED
        await session.flush()

        selected = await routing_service.select_available_agent(
            session, business.id, preferred_agent_id=agent.id
        )
        assert selected is not None and selected.id == second_agent.id

    async def test_falls_back_when_the_preferred_agent_is_at_capacity(
        self, session, business, agent, second_agent, phone_number
    ):
        agent.max_concurrent_calls = 1
        await session.flush()
        await _live_call(session, business, agent, phone_number, 1)

        selected = await routing_service.select_available_agent(
            session, business.id, preferred_agent_id=agent.id
        )
        assert selected is not None and selected.id == second_agent.id

    async def test_returns_none_when_nobody_is_available(self, session, business, agent):
        agent.status = AgentStatus.PAUSED
        await session.flush()

        selected = await routing_service.select_available_agent(session, business.id)
        assert selected is None

    async def test_prefers_the_least_loaded_available_agent_with_no_preference(
        self, session, business, agent, second_agent, phone_number
    ):
        await _live_call(session, business, agent, phone_number, 1)

        selected = await routing_service.select_available_agent(session, business.id)
        assert selected is not None and selected.id == second_agent.id


class TestInboundRoutingFallback:
    async def test_routes_to_a_fallback_agent_when_the_bound_agent_is_paused(
        self, session, agent, second_agent, phone_number
    ):
        agent.status = AgentStatus.PAUSED
        await session.flush()

        call, routed_agent = await call_service.handle_inbound_call(
            session,
            to_number=phone_number.number,
            from_number="+919999988888",
            provider_call_id="prov-fallback-1",
            provider=TelephonyProvider.MOCK,
        )

        assert routed_agent is not None and routed_agent.id == second_agent.id
        assert call.agent_id == second_agent.id

    async def test_no_agent_available_leaves_the_call_unassigned_and_flags_voicemail(
        self, session, agent, phone_number
    ):
        agent.status = AgentStatus.PAUSED
        await session.flush()

        call, routed_agent = await call_service.handle_inbound_call(
            session,
            to_number=phone_number.number,
            from_number="+919999988888",
            provider_call_id="prov-fallback-2",
            provider=TelephonyProvider.MOCK,
        )

        assert routed_agent is None
        assert call.agent_id is None
        assert call.metadata_json["routing"]["voicemail_recommended"] is True


class TestAvailabilityEndpoint:
    async def test_lists_every_agent_with_its_standing(self, client, owner_headers, agent):
        response = await client.get("/api/v1/agents/availability", headers=owner_headers)
        assert response.status_code == 200
        body = response.json()
        assert len(body) == 1
        assert body[0]["agent_id"] == str(agent.id)
        assert body[0]["is_available"] is True

    async def test_reflects_a_paused_agent(self, client, session, owner_headers, agent):
        agent.status = AgentStatus.PAUSED
        await session.flush()

        response = await client.get("/api/v1/agents/availability", headers=owner_headers)
        body = response.json()
        assert body[0]["is_available"] is False
        assert body[0]["reason"] == "agent_paused"

    async def test_tenant_scoped(self, client, other_headers):
        response = await client.get("/api/v1/agents/availability", headers=other_headers)
        assert response.status_code == 200
        assert response.json() == []
