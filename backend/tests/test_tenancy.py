"""Multi-tenant isolation.

Every query in the application must be scoped by ``business_id``. These tests
assert both the helper layer and the HTTP surface: a tenant must never be able
to read, mutate or even detect another tenant's rows.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from app.core.errors import NotFoundError
from app.core.tenancy import get_owned_or_404, tenant_exists, tenant_select
from app.models.business import Business, User
from app.models.call import CallLog
from app.models.voice_agent import VoiceAgent
from tests.conftest import auth_headers


class TestTenantSelect:
    async def test_scopes_to_the_business(self, session, business, other_business):
        for owner_business, name in ((business, "Mine"), (other_business, "Theirs")):
            session.add(
                VoiceAgent(business_id=owner_business.id, name=name, persona="", flow_json={})
            )
        await session.flush()

        rows = (
            (await session.execute(tenant_select(VoiceAgent, business.id))).scalars().all()
        )
        assert [r.name for r in rows] == ["Mine"]

    async def test_excludes_soft_deleted_rows_by_default(self, session, business):
        session.add(
            VoiceAgent(
                business_id=business.id,
                name="Retired",
                persona="",
                flow_json={},
                deleted_at=datetime.now(UTC),
            )
        )
        await session.flush()

        assert (await session.execute(tenant_select(VoiceAgent, business.id))).scalars().all() == []
        included = (
            (
                await session.execute(
                    tenant_select(VoiceAgent, business.id, include_deleted=True)
                )
            )
            .scalars()
            .all()
        )
        assert [r.name for r in included] == ["Retired"]

    def test_rejects_a_model_without_business_id(self):
        # Business itself is the tenant root, so it has no business_id column.
        with pytest.raises(TypeError, match="not a tenant-scoped"):
            tenant_select(Business, uuid.uuid4())


class TestGetOwnedOr404:
    async def test_returns_own_row(self, session, business, agent):
        found = await get_owned_or_404(session, VoiceAgent, agent.id, business.id)
        assert found.id == agent.id

    async def test_another_tenants_row_is_indistinguishable_from_missing(
        self, session, business, other_business, agent
    ):
        """Cross-tenant reads must 404, not 403 — a 403 would confirm existence."""
        with pytest.raises(NotFoundError) as missing:
            await get_owned_or_404(session, VoiceAgent, uuid.uuid4(), other_business.id)
        with pytest.raises(NotFoundError) as foreign:
            await get_owned_or_404(session, VoiceAgent, agent.id, other_business.id)

        assert str(missing.value) == str(foreign.value)

    async def test_soft_deleted_row_is_not_found(self, session, business, agent):
        agent.deleted_at = datetime.now(UTC)
        await session.flush()
        with pytest.raises(NotFoundError):
            await get_owned_or_404(session, VoiceAgent, agent.id, business.id)

    async def test_label_is_used_in_the_message(self, session, other_business):
        with pytest.raises(NotFoundError, match="Phone number not found"):
            await get_owned_or_404(
                session, CallLog, uuid.uuid4(), other_business.id, label="Phone number"
            )


class TestTenantExists:
    async def test_true_for_own_row(self, session, business, agent):
        assert await tenant_exists(session, VoiceAgent, agent.id, business.id)

    async def test_false_across_tenants(self, session, other_business, agent):
        assert not await tenant_exists(session, VoiceAgent, agent.id, other_business.id)


class TestHttpIsolation:
    """The same guarantee, asserted end to end through the API."""

    async def test_agent_list_shows_only_own_agents(
        self, client, session, agent, other_business, other_headers, owner_headers
    ):
        session.add(
            VoiceAgent(
                business_id=other_business.id, name="Rival Agent", persona="", flow_json={}
            )
        )
        await session.flush()

        mine = await client.get("/api/v1/agents", headers=owner_headers)
        theirs = await client.get("/api/v1/agents", headers=other_headers)

        assert [a["name"] for a in mine.json()["items"]] == ["Reception Agent"]
        assert [a["name"] for a in theirs.json()["items"]] == ["Rival Agent"]

    async def test_cross_tenant_agent_fetch_returns_404(
        self, client, agent, other_headers
    ):
        response = await client.get(f"/api/v1/agents/{agent.id}", headers=other_headers)
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found"

    async def test_cross_tenant_agent_update_returns_404(
        self, client, agent, other_headers
    ):
        response = await client.patch(
            f"/api/v1/agents/{agent.id}", headers=other_headers, json={"name": "Hijacked"}
        )
        assert response.status_code == 404

    async def test_cross_tenant_agent_delete_returns_404(
        self, client, session, agent, other_headers
    ):
        response = await client.delete(f"/api/v1/agents/{agent.id}", headers=other_headers)
        assert response.status_code == 404

        await session.refresh(agent)
        assert agent.deleted_at is None, "the row must be untouched"

    async def test_cross_tenant_call_fetch_returns_404(self, client, call, other_headers):
        response = await client.get(f"/api/v1/calls/{call.id}", headers=other_headers)
        assert response.status_code == 404

    async def test_token_business_must_match_the_user(self, client, session, owner, other_business):
        """A token whose business_id was swapped must be rejected outright."""
        from app.core.security import create_access_token

        token, _ = create_access_token(
            user_id=owner.id, business_id=other_business.id, role=owner.role
        )
        response = await client.get(
            "/api/v1/agents", headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code == 401
        assert "does not match" in response.json()["error"]["message"]

    async def test_deactivated_user_is_rejected_even_with_a_valid_token(
        self, client, session, owner
    ):
        headers = auth_headers(owner)
        owner.is_active = False
        await session.flush()

        response = await client.get("/api/v1/agents", headers=headers)
        assert response.status_code == 401
        assert "no longer active" in response.json()["error"]["message"]

    async def test_soft_deleted_user_is_rejected(self, client, session, owner):
        headers = auth_headers(owner)
        owner.deleted_at = datetime.now(UTC)
        await session.flush()

        assert (await client.get("/api/v1/agents", headers=headers)).status_code == 401

    async def test_missing_token_is_rejected(self, client):
        response = await client.get("/api/v1/agents")
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "authentication_error"


class TestRoleGating:
    async def test_viewer_cannot_create_an_agent(self, client, viewer_headers):
        response = await client.post(
            "/api/v1/agents", headers=viewer_headers, json={"name": "Nope"}
        )
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "permission_denied"

    async def test_viewer_can_read_agents(self, client, agent, viewer_headers):
        response = await client.get("/api/v1/agents", headers=viewer_headers)
        assert response.status_code == 200

    async def test_viewer_cannot_initiate_a_call(self, client, agent, viewer_headers):
        response = await client.post(
            "/api/v1/calls/initiate",
            headers=viewer_headers,
            json={"agent_id": str(agent.id), "to_number": "+919999900000"},
        )
        assert response.status_code == 403

    async def test_owner_can_create_an_agent(self, client, owner_headers):
        response = await client.post(
            "/api/v1/agents", headers=owner_headers, json={"name": "New Agent"}
        )
        assert response.status_code == 201


class TestUserScoping:
    async def test_users_are_unique_per_business_not_globally(
        self, session, business, other_business
    ):
        """The same email may sign up with two different businesses."""
        shared = "shared@example.test"
        for tenant in (business, other_business):
            session.add(
                User(
                    business_id=tenant.id,
                    email=shared,
                    full_name="Shared",
                    password_hash="x",
                    role="owner",
                )
            )
        await session.flush()  # must not raise
