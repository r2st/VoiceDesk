"""Number provisioning, binding, release and line rent (design doc §5.1).

A phone number is the one piece of tenant state that also exists at the
telephony provider, so most of what matters here is what happens when the two
disagree: the provider fails mid-provision, or has already reclaimed a number
the platform still thinks it holds. The row is the audit trail either way, so
these tests are mostly about it never silently vanishing.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, ExternalServiceError, NotFoundError, ValidationError
from app.models.business import Business
from app.models.call import CallLog, PhoneNumber
from app.models.enums import (
    CallDirection,
    CallStatus,
    PhoneNumberStatus,
    TelephonyProvider,
)
from app.models.voice_agent import VoiceAgent
from app.schemas.call import PhoneNumberUpdate, ProvisionNumberRequest
from app.services import phone_number_service, telephony


async def provision(session: AsyncSession, business: Business, **kwargs) -> PhoneNumber:
    return await phone_number_service.provision_number(
        session, business.id, ProvisionNumberRequest(**kwargs)
    )


async def add_call(
    session: AsyncSession,
    business: Business,
    number: PhoneNumber,
    status: CallStatus,
) -> CallLog:
    row = CallLog(
        business_id=business.id,
        phone_number_id=number.id,
        direction=CallDirection.INBOUND,
        status=status,
        caller_number="+919999900000",
        callee_number=number.number,
        provider=TelephonyProvider.MOCK,
        started_at=datetime.now(UTC),
    )
    session.add(row)
    await session.flush()
    return row


# --------------------------------------------------------------------------- #
# Provisioning
# --------------------------------------------------------------------------- #
class TestProvisioning:
    async def test_allocates_from_the_provider_and_activates(
        self, session: AsyncSession, business: Business, mock_telephony
    ):
        number = await provision(session, business, region="Bengaluru")

        assert number.status == PhoneNumberStatus.ACTIVE
        assert number.number.startswith("+91")
        assert number.provider_number_id
        assert number.monthly_rent_paise == 49_900
        assert number.business_id == business.id
        assert {i["action"] for i in mock_telephony.interactions} == {"provision_number"}

    async def test_a_requested_number_is_honoured(self, session: AsyncSession, business: Business):
        number = await provision(session, business, number="9876500123")

        # The schema normalises to E.164 before the provider ever sees it.
        assert number.number == "+919876500123"

    async def test_region_falls_back_to_what_the_provider_returned(
        self, session: AsyncSession, business: Business
    ):
        number = await provision(session, business)
        assert number.region == "Bengaluru"

    async def test_binding_to_an_agent_at_provision_time(
        self, session: AsyncSession, business: Business, agent: VoiceAgent
    ):
        number = await provision(session, business, agent_id=agent.id)
        assert number.agent_id == agent.id

    async def test_an_unknown_agent_is_rejected(self, session: AsyncSession, business: Business):
        with pytest.raises(NotFoundError):
            await provision(session, business, agent_id=uuid.uuid4())

    async def test_another_tenants_agent_is_rejected(
        self, session: AsyncSession, business: Business, other_business: Business
    ):
        stranger = VoiceAgent(
            business_id=other_business.id,
            name="Rival Agent",
            use_case="appointment_booking",
        )
        session.add(stranger)
        await session.flush()

        with pytest.raises(NotFoundError):
            await provision(session, business, agent_id=stranger.id)

    async def test_the_same_number_cannot_be_provisioned_twice(
        self, session: AsyncSession, business: Business, phone_number: PhoneNumber
    ):
        with pytest.raises(ConflictError, match="already provisioned"):
            await provision(session, business, number=phone_number.number)

    async def test_the_conflict_reaches_across_tenants(
        self, session: AsyncSession, other_business: Business, phone_number: PhoneNumber
    ):
        # A number belongs to the network, not to a tenant: two businesses
        # cannot hold the same line.
        with pytest.raises(ConflictError):
            await provision(session, other_business, number=phone_number.number)

    async def test_a_released_number_may_be_taken_again(
        self, session: AsyncSession, business: Business, phone_number: PhoneNumber
    ):
        released = phone_number.number
        await phone_number_service.release_number(session, business.id, phone_number.id)

        again = await provision(session, business, number=released)
        assert again.status == PhoneNumberStatus.ACTIVE
        assert again.id != phone_number.id

    async def test_a_provider_failure_leaves_a_failed_row(
        self, session: AsyncSession, business: Business, mock_telephony, monkeypatch
    ):
        """The row survives the failure; that is the point of writing it first."""

        async def explode(**_kwargs):
            raise RuntimeError("carrier rejected the request")

        monkeypatch.setattr(mock_telephony, "provision_number", explode)

        with pytest.raises(ExternalServiceError, match="could not allocate"):
            await provision(session, business, region="Pune")

        numbers, total = await phone_number_service.list_numbers(session, business.id)
        assert total == 1
        assert numbers[0].status == PhoneNumberStatus.FAILED
        assert numbers[0].region == "Pune"

    async def test_a_provider_handing_back_a_held_number_is_a_conflict(
        self,
        session: AsyncSession,
        business: Business,
        phone_number: PhoneNumber,
        mock_telephony,
        monkeypatch,
    ):
        """The provider picks the number when the caller does not.

        If it picks one the platform still holds — most often after a release
        only the provider recorded — that has to read as a conflict rather than
        as a unique-index violation surfacing from the driver.
        """

        async def hand_back_a_held_line(*, region=None, number=None):
            from app.services.telephony.base import ProvisionedNumber

            return ProvisionedNumber(
                number=phone_number.number,
                provider_number_id="mock-num-dup",
                region=region,
                monthly_rent_paise=49_900,
                raw={},
            )

        monkeypatch.setattr(mock_telephony, "provision_number", hand_back_a_held_line)

        with pytest.raises(ConflictError, match="already provisioned"):
            await provision(session, business)

        _, total = await phone_number_service.list_numbers(
            session, business.id, status=PhoneNumberStatus.FAILED
        )
        assert total == 1

    async def test_a_failed_row_holds_no_provider_id(
        self, session: AsyncSession, business: Business, mock_telephony, monkeypatch
    ):
        async def explode(**_kwargs):
            raise RuntimeError("carrier rejected the request")

        monkeypatch.setattr(mock_telephony, "provision_number", explode)
        with pytest.raises(ExternalServiceError):
            await provision(session, business)

        numbers, _ = await phone_number_service.list_numbers(session, business.id)
        assert numbers[0].provider_number_id is None


# --------------------------------------------------------------------------- #
# Listing and reading
# --------------------------------------------------------------------------- #
class TestListing:
    async def test_lists_only_this_tenants_numbers(
        self, session: AsyncSession, business: Business, other_business: Business
    ):
        await provision(session, business)
        await provision(session, other_business)

        _, total = await phone_number_service.list_numbers(session, business.id)
        assert total == 1

    async def test_filters_by_status(
        self, session: AsyncSession, business: Business, mock_telephony, monkeypatch
    ):
        await provision(session, business)

        async def explode(**_kwargs):
            raise RuntimeError("nope")

        monkeypatch.setattr(mock_telephony, "provision_number", explode)
        with pytest.raises(ExternalServiceError):
            await provision(session, business)

        _, active = await phone_number_service.list_numbers(
            session, business.id, status=PhoneNumberStatus.ACTIVE
        )
        _, failed = await phone_number_service.list_numbers(
            session, business.id, status=PhoneNumberStatus.FAILED
        )
        assert (active, failed) == (1, 1)

    async def test_filters_by_agent(
        self, session: AsyncSession, business: Business, agent: VoiceAgent
    ):
        await provision(session, business, agent_id=agent.id)
        await provision(session, business)

        _, bound = await phone_number_service.list_numbers(session, business.id, agent_id=agent.id)
        assert bound == 1

    async def test_pagination_reports_the_full_total(
        self, session: AsyncSession, business: Business
    ):
        for _ in range(3):
            await provision(session, business)

        page, total = await phone_number_service.list_numbers(session, business.id, limit=2)
        assert (len(page), total) == (2, 3)

        second, _ = await phone_number_service.list_numbers(session, business.id, limit=2, offset=2)
        assert len(second) == 1

    async def test_released_numbers_drop_out_of_the_inventory(
        self, session: AsyncSession, business: Business, phone_number: PhoneNumber
    ):
        await phone_number_service.release_number(session, business.id, phone_number.id)

        _, total = await phone_number_service.list_numbers(session, business.id)
        assert total == 0

    async def test_reading_another_tenants_number_is_a_404(
        self, session: AsyncSession, other_business: Business, phone_number: PhoneNumber
    ):
        with pytest.raises(NotFoundError):
            await phone_number_service.get_number(session, other_business.id, phone_number.id)


# --------------------------------------------------------------------------- #
# Rebinding
# --------------------------------------------------------------------------- #
class TestUpdate:
    async def test_toggles_leave_the_binding_alone(
        self, session: AsyncSession, business: Business, phone_number: PhoneNumber
    ):
        bound_to = phone_number.agent_id
        updated = await phone_number_service.update_number(
            session, business.id, phone_number.id, PhoneNumberUpdate(outbound_enabled=False)
        )

        assert updated.outbound_enabled is False
        assert updated.inbound_enabled is True
        assert updated.agent_id == bound_to

    async def test_unbinding_is_expressible(
        self, session: AsyncSession, business: Business, phone_number: PhoneNumber
    ):
        updated = await phone_number_service.update_number(
            session, business.id, phone_number.id, PhoneNumberUpdate(agent_id=None)
        )
        assert updated.agent_id is None

    async def test_cannot_bind_to_another_tenants_agent(
        self,
        session: AsyncSession,
        business: Business,
        other_business: Business,
        phone_number: PhoneNumber,
    ):
        stranger = VoiceAgent(
            business_id=other_business.id, name="Rival Agent", use_case="appointment_booking"
        )
        session.add(stranger)
        await session.flush()

        with pytest.raises(NotFoundError):
            await phone_number_service.update_number(
                session, business.id, phone_number.id, PhoneNumberUpdate(agent_id=stranger.id)
            )

    async def test_updating_another_tenants_number_is_a_404(
        self, session: AsyncSession, other_business: Business, phone_number: PhoneNumber
    ):
        with pytest.raises(NotFoundError):
            await phone_number_service.update_number(
                session,
                other_business.id,
                phone_number.id,
                PhoneNumberUpdate(inbound_enabled=False),
            )


class TestAssignAgent:
    async def test_points_the_line_at_an_agent(
        self, session: AsyncSession, business: Business, agent: VoiceAgent
    ):
        number = await provision(session, business)
        assigned = await phone_number_service.assign_agent(
            session, business.id, number.id, agent.id
        )
        assert assigned.agent_id == agent.id

    async def test_a_provisioning_number_cannot_take_calls_yet(
        self, session: AsyncSession, business: Business, agent: VoiceAgent
    ):
        pending = PhoneNumber(
            business_id=business.id,
            number="+918000000099",
            provider=TelephonyProvider.MOCK,
            status=PhoneNumberStatus.PROVISIONING,
        )
        session.add(pending)
        await session.flush()

        with pytest.raises(ValidationError, match="only active numbers"):
            await phone_number_service.assign_agent(session, business.id, pending.id, agent.id)

    async def test_a_released_number_cannot_be_reassigned(
        self,
        session: AsyncSession,
        business: Business,
        agent: VoiceAgent,
        phone_number: PhoneNumber,
    ):
        await phone_number_service.release_number(session, business.id, phone_number.id)

        # Soft deleted, so it is not even visible to look up any more.
        with pytest.raises(NotFoundError):
            await phone_number_service.assign_agent(session, business.id, phone_number.id, agent.id)

    async def test_an_unknown_agent_is_rejected(
        self, session: AsyncSession, business: Business, phone_number: PhoneNumber
    ):
        with pytest.raises(NotFoundError):
            await phone_number_service.assign_agent(
                session, business.id, phone_number.id, uuid.uuid4()
            )


# --------------------------------------------------------------------------- #
# Release
# --------------------------------------------------------------------------- #
class TestRelease:
    async def test_release_hands_the_number_back_and_soft_deletes(
        self, session: AsyncSession, business: Business, phone_number: PhoneNumber, mock_telephony
    ):
        released = await phone_number_service.release_number(session, business.id, phone_number.id)

        assert released.status == PhoneNumberStatus.RELEASED
        assert released.inbound_enabled is False
        assert released.outbound_enabled is False
        assert released.deleted_at is not None
        assert {"action": "release_number", "provider_number_id": "mock-num-1"} in (
            mock_telephony.interactions
        )

    async def test_a_live_call_blocks_the_release(
        self, session: AsyncSession, business: Business, phone_number: PhoneNumber
    ):
        await add_call(session, business, phone_number, CallStatus.IN_PROGRESS)

        with pytest.raises(ConflictError, match="still active"):
            await phone_number_service.release_number(session, business.id, phone_number.id)

    @pytest.mark.parametrize(
        "status", [CallStatus.QUEUED, CallStatus.RINGING, CallStatus.IN_PROGRESS]
    )
    async def test_every_pre_terminal_state_blocks_the_release(
        self,
        session: AsyncSession,
        business: Business,
        phone_number: PhoneNumber,
        status: CallStatus,
    ):
        await add_call(session, business, phone_number, status)

        with pytest.raises(ConflictError):
            await phone_number_service.release_number(session, business.id, phone_number.id)

    async def test_a_finished_call_does_not_block(
        self, session: AsyncSession, business: Business, phone_number: PhoneNumber
    ):
        await add_call(session, business, phone_number, CallStatus.COMPLETED)

        released = await phone_number_service.release_number(session, business.id, phone_number.id)
        assert released.status == PhoneNumberStatus.RELEASED

    async def test_release_survives_a_provider_that_already_reclaimed_it(
        self,
        session: AsyncSession,
        business: Business,
        phone_number: PhoneNumber,
        mock_telephony,
        monkeypatch,
    ):
        """The provider is the less authoritative side here.

        If it errors — most likely because it has already dropped the number —
        the platform still has to let go, or the line stays on the bill forever.
        """

        async def explode(_provider_number_id: str):
            raise RuntimeError("unknown number")

        monkeypatch.setattr(mock_telephony, "release_number", explode)

        released = await phone_number_service.release_number(session, business.id, phone_number.id)
        assert released.status == PhoneNumberStatus.RELEASED

    async def test_a_number_never_allocated_upstream_skips_the_provider(
        self, session: AsyncSession, business: Business, mock_telephony
    ):
        stalled = PhoneNumber(
            business_id=business.id,
            number="+918000000077",
            provider=TelephonyProvider.MOCK,
            status=PhoneNumberStatus.FAILED,
        )
        session.add(stalled)
        await session.flush()

        await phone_number_service.release_number(session, business.id, stalled.id)
        assert not [i for i in mock_telephony.interactions if i["action"] == "release_number"]

    async def test_releasing_another_tenants_number_is_a_404(
        self, session: AsyncSession, other_business: Business, phone_number: PhoneNumber
    ):
        with pytest.raises(NotFoundError):
            await phone_number_service.release_number(session, other_business.id, phone_number.id)

    async def test_releasing_twice_is_a_404_the_second_time(
        self, session: AsyncSession, business: Business, phone_number: PhoneNumber
    ):
        await phone_number_service.release_number(session, business.id, phone_number.id)

        with pytest.raises(NotFoundError):
            await phone_number_service.release_number(session, business.id, phone_number.id)


# --------------------------------------------------------------------------- #
# Line rent
# --------------------------------------------------------------------------- #
class TestMonthlyRent:
    async def test_sums_active_lines(
        self, session: AsyncSession, business: Business, phone_number: PhoneNumber
    ):
        second = await provision(session, business, number="9876500555")

        total = await phone_number_service.monthly_rent_paise(session, business.id)
        assert total == phone_number.monthly_rent_paise + second.monthly_rent_paise
        assert total == 15_000 + 49_900

    async def test_a_released_line_stops_costing(
        self, session: AsyncSession, business: Business, phone_number: PhoneNumber
    ):
        await phone_number_service.release_number(session, business.id, phone_number.id)
        assert await phone_number_service.monthly_rent_paise(session, business.id) == 0

    async def test_a_failed_line_never_costs(
        self, session: AsyncSession, business: Business, mock_telephony, monkeypatch
    ):
        async def explode(**_kwargs):
            raise RuntimeError("nope")

        monkeypatch.setattr(mock_telephony, "provision_number", explode)
        with pytest.raises(ExternalServiceError):
            await provision(session, business)

        assert await phone_number_service.monthly_rent_paise(session, business.id) == 0

    async def test_another_tenants_lines_are_not_billed_here(
        self, session: AsyncSession, business: Business, other_business: Business
    ):
        await provision(session, other_business)
        assert await phone_number_service.monthly_rent_paise(session, business.id) == 0

    async def test_no_numbers_is_zero_not_null(self, session: AsyncSession, business: Business):
        assert await phone_number_service.monthly_rent_paise(session, business.id) == 0


# --------------------------------------------------------------------------- #
# HTTP surface
# --------------------------------------------------------------------------- #
class TestEndpoints:
    async def test_provision_over_http(self, client: AsyncClient, owner_headers: dict):
        response = await client.post(
            "/api/v1/phone-numbers/provision",
            headers=owner_headers,
            json={"region": "Bengaluru"},
        )
        assert response.status_code == 201
        assert response.json()["status"] == "active"

    async def test_a_viewer_cannot_provision(self, client: AsyncClient, viewer_headers: dict):
        response = await client.post(
            "/api/v1/phone-numbers/provision", headers=viewer_headers, json={}
        )
        assert response.status_code == 403

    async def test_a_supervisor_cannot_provision(
        self, client: AsyncClient, supervisor_headers: dict
    ):
        response = await client.post(
            "/api/v1/phone-numbers/provision", headers=supervisor_headers, json={}
        )
        assert response.status_code == 403

    async def test_a_viewer_may_read_the_inventory(
        self, client: AsyncClient, viewer_headers: dict, phone_number: PhoneNumber
    ):
        listing = await client.get("/api/v1/phone-numbers", headers=viewer_headers)
        assert listing.status_code == 200
        assert listing.json()["total"] == 1

        detail = await client.get(
            f"/api/v1/phone-numbers/{phone_number.id}", headers=viewer_headers
        )
        assert detail.status_code == 200
        assert detail.json()["number"] == phone_number.number

    async def test_status_filter_over_http(
        self, client: AsyncClient, owner_headers: dict, phone_number: PhoneNumber
    ):
        # The tenant holds exactly one line, and it is active — so filtering
        # for released must not fall back to "everything".
        assert phone_number.status == PhoneNumberStatus.ACTIVE

        response = await client.get(
            "/api/v1/phone-numbers", headers=owner_headers, params={"status": "released"}
        )
        assert response.json()["total"] == 0

    async def test_an_invalid_status_filter_is_a_422(
        self, client: AsyncClient, owner_headers: dict
    ):
        response = await client.get(
            "/api/v1/phone-numbers", headers=owner_headers, params={"status": "melted"}
        )
        assert response.status_code == 422

    async def test_assign_over_http(
        self,
        client: AsyncClient,
        owner_headers: dict,
        phone_number: PhoneNumber,
        agent: VoiceAgent,
    ):
        response = await client.post(
            f"/api/v1/phone-numbers/{phone_number.id}/assign",
            headers=owner_headers,
            json={"agent_id": str(agent.id)},
        )
        assert response.status_code == 200
        assert response.json()["agent_id"] == str(agent.id)

    async def test_a_viewer_cannot_assign(
        self,
        client: AsyncClient,
        viewer_headers: dict,
        phone_number: PhoneNumber,
        agent: VoiceAgent,
    ):
        response = await client.post(
            f"/api/v1/phone-numbers/{phone_number.id}/assign",
            headers=viewer_headers,
            json={"agent_id": str(agent.id)},
        )
        assert response.status_code == 403

    async def test_patch_over_http(
        self, client: AsyncClient, owner_headers: dict, phone_number: PhoneNumber
    ):
        response = await client.patch(
            f"/api/v1/phone-numbers/{phone_number.id}",
            headers=owner_headers,
            json={"inbound_enabled": False},
        )
        assert response.status_code == 200
        assert response.json()["inbound_enabled"] is False

    async def test_release_over_http(
        self, client: AsyncClient, owner_headers: dict, phone_number: PhoneNumber
    ):
        response = await client.delete(
            f"/api/v1/phone-numbers/{phone_number.id}", headers=owner_headers
        )
        assert response.status_code == 200
        assert response.json()["status"] == "released"

    async def test_a_viewer_cannot_release(
        self, client: AsyncClient, viewer_headers: dict, phone_number: PhoneNumber
    ):
        response = await client.delete(
            f"/api/v1/phone-numbers/{phone_number.id}", headers=viewer_headers
        )
        assert response.status_code == 403

    async def test_release_with_a_live_call_is_a_409(
        self,
        client: AsyncClient,
        owner_headers: dict,
        session: AsyncSession,
        business: Business,
        phone_number: PhoneNumber,
    ):
        await add_call(session, business, phone_number, CallStatus.RINGING)

        response = await client.delete(
            f"/api/v1/phone-numbers/{phone_number.id}", headers=owner_headers
        )
        assert response.status_code == 409

    async def test_a_number_from_another_tenant_is_a_404(
        self, client: AsyncClient, other_headers: dict, phone_number: PhoneNumber
    ):
        response = await client.get(
            f"/api/v1/phone-numbers/{phone_number.id}", headers=other_headers
        )
        assert response.status_code == 404


# --------------------------------------------------------------------------- #
# Provider registry
# --------------------------------------------------------------------------- #
class TestProviderRegistry:
    def test_an_override_wins_over_the_name(self, mock_telephony):
        assert telephony.get_provider("exotel") is mock_telephony

    def test_named_providers_resolve_and_are_cached(self):
        telephony.set_provider_override(None)
        try:
            exotel = telephony.get_provider("exotel")
            assert exotel.name == "exotel"
            assert telephony.get_provider("exotel") is exotel

            assert telephony.get_provider("knowlarity").name == "knowlarity"
            # Anything unrecognised falls back to the mock rather than failing
            # a call at dial time.
            assert telephony.get_provider("carrier-pigeon").name == "mock"
        finally:
            telephony.reset_providers()
