"""Voice agent and intent CRUD, plus plan-tier limits (design doc §4.1, §6.1)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.models.call import CallLog
from app.models.enums import (
    AgentStatus,
    AgentUseCase,
    CallDirection,
    CallStatus,
    IntentActionType,
    Language,
    PlanTier,
)
from app.models.voice_agent import VoiceAgent
from app.schemas.agent import IntentCreate, IntentUpdate, VoiceAgentCreate, VoiceAgentUpdate
from app.services import agent_service


def make(**overrides) -> VoiceAgentCreate:
    return VoiceAgentCreate(**{"name": "Support Agent", **overrides})


class TestCreateAgent:
    async def test_creates_with_a_default_flow_and_greeting(self, session, business):
        agent = await agent_service.create_agent(session, business.id, make())

        assert agent.business_id == business.id
        assert agent.flow_version == 1
        assert agent.flow_json["start_node"] == "greeting"
        assert agent.greeting, "a greeting is generated when none is supplied"

    async def test_greeting_defaults_to_the_agent_language(self, session, business):
        hindi = await agent_service.create_agent(
            session, business.id, make(name="Hindi", language=Language.HINDI)
        )
        tamil = await agent_service.create_agent(
            session, business.id, make(name="Tamil", language=Language.TAMIL)
        )
        assert hindi.greeting != tamil.greeting

    async def test_explicit_greeting_wins(self, session, business):
        agent = await agent_service.create_agent(
            session, business.id, make(greeting="Custom hello")
        )
        assert agent.greeting == "Custom hello"
        assert agent.flow_json["nodes"][0]["text"] == "Custom hello"

    async def test_duplicate_name_within_a_tenant_is_rejected(self, session, business):
        await agent_service.create_agent(session, business.id, make())
        with pytest.raises(ConflictError, match="already exists"):
            await agent_service.create_agent(session, business.id, make())

    async def test_same_name_in_another_tenant_is_fine(self, session, business, other_business):
        await agent_service.create_agent(session, business.id, make())
        agent = await agent_service.create_agent(session, other_business.id, make())
        assert agent.business_id == other_business.id

    async def test_a_soft_deleted_name_can_be_reused(self, session, business):
        first = await agent_service.create_agent(session, business.id, make())
        first.deleted_at = datetime.now(UTC)
        await session.flush()

        again = await agent_service.create_agent(session, business.id, make())
        assert again.id != first.id

    async def test_invalid_flow_is_rejected(self, session, business):
        with pytest.raises(ValidationError):
            await agent_service.create_agent(
                session,
                business.id,
                make(flow_json={"start_node": "a", "nodes": [{"id": "b", "type": "end"}]}),
            )

    async def test_a_persona_is_generated_when_omitted(self, session, business):
        agent = await agent_service.create_agent(
            session, business.id, make(use_case=AgentUseCase.PAYMENT_REMINDER)
        )
        assert agent.persona


class TestPlanLimits:
    async def test_starter_plan_allows_only_one_agent(self, session, business):
        business.plan = PlanTier.STARTER
        await session.flush()

        await agent_service.create_agent(session, business.id, make(name="First"))
        with pytest.raises(ValidationError, match="allows 1 agent"):
            await agent_service.create_agent(session, business.id, make(name="Second"))

    async def test_starter_plan_allows_only_one_language(self, session, business):
        business.plan = PlanTier.STARTER
        await session.flush()

        with pytest.raises(ValidationError, match="language"):
            await agent_service.create_agent(
                session,
                business.id,
                make(
                    language=Language.HINDI,
                    supported_languages=[Language.ENGLISH, Language.TAMIL],
                ),
            )

    async def test_business_plan_has_no_agent_cap(self, session, business):
        business.plan = PlanTier.BUSINESS
        await session.flush()

        for index in range(4):
            await agent_service.create_agent(session, business.id, make(name=f"Agent {index}"))

        agents, total = await agent_service.list_agents(session, business.id)
        assert total == 4

    async def test_soft_deleted_agents_do_not_count_towards_the_cap(self, session, business):
        business.plan = PlanTier.STARTER
        await session.flush()

        first = await agent_service.create_agent(session, business.id, make(name="First"))
        first.deleted_at = datetime.now(UTC)
        await session.flush()

        await agent_service.create_agent(session, business.id, make(name="Second"))


class TestUpdateAgent:
    async def test_patches_only_supplied_fields(self, session, business, agent):
        original_persona = agent.persona
        updated = await agent_service.update_agent(
            session, business.id, agent.id, VoiceAgentUpdate(name="Renamed")
        )
        assert updated.name == "Renamed"
        assert updated.persona == original_persona

    async def test_activating_an_agent(self, session, business, agent):
        agent.status = AgentStatus.DRAFT
        await session.flush()

        updated = await agent_service.update_agent(
            session, business.id, agent.id, VoiceAgentUpdate(status=AgentStatus.ACTIVE)
        )
        assert updated.status == AgentStatus.ACTIVE

    async def test_rename_collision_is_rejected(self, session, business, agent):
        await agent_service.create_agent(session, business.id, make(name="Taken"))
        with pytest.raises(ConflictError):
            await agent_service.update_agent(
                session, business.id, agent.id, VoiceAgentUpdate(name="Taken")
            )

    async def test_cross_tenant_update_is_not_found(self, session, other_business, agent):
        with pytest.raises(NotFoundError):
            await agent_service.update_agent(
                session, other_business.id, agent.id, VoiceAgentUpdate(name="Hijacked")
            )


class TestDuplicateAgent:
    async def test_clone_copies_the_flow_and_starts_as_a_draft(self, session, business, agent):
        clone = await agent_service.duplicate_agent(session, business.id, agent.id, None)

        assert clone.id != agent.id
        assert clone.flow_json == agent.flow_json
        assert clone.status == AgentStatus.DRAFT
        assert clone.name != agent.name

    async def test_clone_accepts_an_explicit_name(self, session, business, agent):
        clone = await agent_service.duplicate_agent(session, business.id, agent.id, "Night Shift")
        assert clone.name == "Night Shift"


class TestDeleteAgent:
    async def test_soft_delete_retains_the_row(self, session, business, agent):
        await agent_service.soft_delete_agent(session, business.id, agent.id)

        row = await session.get(VoiceAgent, agent.id)
        assert row is not None and row.deleted_at is not None

    async def test_refuses_while_calls_are_live(self, session, business, agent):
        session.add(
            CallLog(
                business_id=business.id,
                agent_id=agent.id,
                direction=CallDirection.INBOUND,
                status=CallStatus.IN_PROGRESS,
                caller_number="+919999900000",
                callee_number="+918000000001",
            )
        )
        await session.flush()

        with pytest.raises(ConflictError):
            await agent_service.soft_delete_agent(session, business.id, agent.id)

    async def test_deleted_agent_is_gone_from_listings(self, session, business, agent):
        await agent_service.soft_delete_agent(session, business.id, agent.id)
        _, total = await agent_service.list_agents(session, business.id)
        assert total == 0


class TestListAgents:
    async def test_filters_by_status(self, session, business, agent):
        await agent_service.create_agent(
            session, business.id, make(name="Draft One", status=AgentStatus.DRAFT)
        )
        _, active = await agent_service.list_agents(session, business.id, status=AgentStatus.ACTIVE)
        _, drafts = await agent_service.list_agents(session, business.id, status=AgentStatus.DRAFT)
        assert (active, drafts) == (1, 1)

    async def test_search_matches_the_name(self, session, business, agent):
        await agent_service.create_agent(session, business.id, make(name="Billing Desk"))
        _, total = await agent_service.list_agents(session, business.id, search="billing")
        assert total == 1


class TestIntents:
    async def test_create_and_list(self, session, business, agent):
        intent = await agent_service.create_intent(
            session,
            business.id,
            IntentCreate(
                name="book_appointment",
                description="Caller wants a slot",
                sample_phrases=["appointment chahiye"],
                action_type=IntentActionType.BOOK_APPOINTMENT,
                agent_id=agent.id,
            ),
        )
        assert intent.business_id == business.id

        intents = await agent_service.list_intents(session, business.id, agent_id=agent.id)
        assert [i.name for i in intents] == ["book_appointment"]

    async def test_name_must_be_snake_case(self):
        with pytest.raises(Exception):
            IntentCreate(name="Book Appointment")

    async def test_duplicate_intent_name_in_the_same_scope_is_rejected(
        self, session, business, agent
    ):
        payload = IntentCreate(name="book_appointment", agent_id=agent.id)
        await agent_service.create_intent(session, business.id, payload)
        with pytest.raises(ConflictError):
            await agent_service.create_intent(session, business.id, payload)

    async def test_active_only_filter(self, session, business):
        await agent_service.create_intent(
            session, business.id, IntentCreate(name="live_one", is_active=True)
        )
        inactive = await agent_service.create_intent(
            session, business.id, IntentCreate(name="dormant_one", is_active=False)
        )
        assert inactive.is_active is False

        active = await agent_service.list_intents(session, business.id, active_only=True)
        assert [i.name for i in active] == ["live_one"]

    async def test_update_and_soft_delete(self, session, business):
        intent = await agent_service.create_intent(
            session, business.id, IntentCreate(name="check_status")
        )
        updated = await agent_service.update_intent(
            session, business.id, intent.id, IntentUpdate(priority=5)
        )
        assert updated.priority == 5

        await agent_service.soft_delete_intent(session, business.id, intent.id)
        assert await agent_service.list_intents(session, business.id) == []


class TestAgentEndpoints:
    async def test_create_via_http(self, client, owner_headers):
        response = await client.post(
            "/api/v1/agents",
            headers=owner_headers,
            json={"name": "HTTP Agent", "language": "en", "use_case": "lead_qualification"},
        )
        assert response.status_code == 201
        body = response.json()
        assert body["name"] == "HTTP Agent"
        assert body["flow_json"]["start_node"] == "greeting"

    async def test_duplicate_name_returns_409(self, client, owner_headers, agent):
        response = await client.post(
            "/api/v1/agents", headers=owner_headers, json={"name": agent.name}
        )
        assert response.status_code == 409

    async def test_short_name_is_rejected(self, client, owner_headers):
        response = await client.post("/api/v1/agents", headers=owner_headers, json={"name": "x"})
        assert response.status_code == 422

    async def test_pagination_envelope(self, client, owner_headers, agent):
        response = await client.get("/api/v1/agents?limit=1&offset=0", headers=owner_headers)
        body = response.json()
        assert set(body) == {"items", "total", "limit", "offset"}

    async def test_unknown_agent_is_404(self, client, owner_headers):
        response = await client.get(f"/api/v1/agents/{uuid.uuid4()}", headers=owner_headers)
        assert response.status_code == 404

    async def test_delete_returns_204(self, client, owner_headers, agent):
        response = await client.delete(f"/api/v1/agents/{agent.id}", headers=owner_headers)
        assert response.status_code == 204

    async def test_duplicate_endpoint(self, client, owner_headers, agent):
        response = await client.post(
            f"/api/v1/agents/{agent.id}/duplicate",
            headers=owner_headers,
            json={"name": "Cloned"},
        )
        assert response.status_code == 201
        assert response.json()["name"] == "Cloned"
