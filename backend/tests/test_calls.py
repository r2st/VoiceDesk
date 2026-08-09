"""Call orchestration: initiation, compliance gating, lifecycle and queries."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.core.errors import ComplianceError, ConflictError, ValidationError
from app.models.call import CallLog, PhoneNumber
from app.models.enums import (
    AgentStatus,
    CallDirection,
    CallResolution,
    CallStatus,
    PhoneNumberStatus,
    TelephonyProvider,
)
from app.schemas.call import InitiateCallRequest
from app.services import call_service, compliance, recording_service
from app.services.conversation_engine import get_engine

IST = ZoneInfo("Asia/Kolkata")


def in_hours(days_ahead: int = 1) -> datetime:
    """A future timestamp that always lands inside TRAI calling hours."""
    target = datetime.now(IST) + timedelta(days=days_ahead)
    return target.replace(hour=11, minute=0, second=0, microsecond=0)


class TestInitiateCall:
    async def test_places_a_call_through_the_provider(
        self, session, business, agent, phone_number, mock_telephony
    ):
        call = await call_service.initiate_call(
            session,
            business.id,
            InitiateCallRequest(agent_id=agent.id, to_number="9876500000"),
        )

        assert call.direction == CallDirection.OUTBOUND
        assert call.caller_number == "+919876500000"
        assert call.callee_number == phone_number.number
        assert call.provider_call_id
        assert call.status in {CallStatus.RINGING, CallStatus.IN_PROGRESS, CallStatus.QUEUED}

    async def test_rejects_an_inactive_agent(self, session, business, agent, phone_number):
        agent.status = AgentStatus.DRAFT
        await session.flush()

        with pytest.raises(ValidationError, match="activate it"):
            await call_service.initiate_call(
                session,
                business.id,
                InitiateCallRequest(agent_id=agent.id, to_number="9876500000"),
            )

    async def test_rejects_an_agent_from_another_tenant(self, session, other_business, agent):
        from app.core.errors import NotFoundError

        with pytest.raises(NotFoundError):
            await call_service.initiate_call(
                session,
                other_business.id,
                InitiateCallRequest(agent_id=agent.id, to_number="9876500000"),
            )

    async def test_requires_a_provisioned_outbound_number(self, session, business, agent):
        with pytest.raises(ValidationError, match="No active outbound phone number"):
            await call_service.initiate_call(
                session,
                business.id,
                InitiateCallRequest(agent_id=agent.id, to_number="9876500000"),
            )

    async def test_prefers_a_number_already_bound_to_the_agent(
        self, session, business, agent, phone_number
    ):
        # An older unbound number exists; the agent's own line must still win.
        session.add(
            PhoneNumber(
                business_id=business.id,
                number="+918000000099",
                provider=TelephonyProvider.MOCK,
                status=PhoneNumberStatus.ACTIVE,
                created_at=datetime.now(UTC) - timedelta(days=30),
            )
        )
        await session.flush()

        call = await call_service.initiate_call(
            session,
            business.id,
            InitiateCallRequest(agent_id=agent.id, to_number="9876500000"),
        )
        assert call.callee_number == phone_number.number

    async def test_explicit_from_number_must_be_outbound_enabled(
        self, session, business, agent, phone_number
    ):
        phone_number.outbound_enabled = False
        await session.flush()

        with pytest.raises(ValidationError, match="Outbound calling is disabled"):
            await call_service.initiate_call(
                session,
                business.id,
                InitiateCallRequest(
                    agent_id=agent.id, to_number="9876500000", from_number_id=phone_number.id
                ),
            )

    async def test_dnd_number_is_blocked_and_recorded(self, session, business, agent, phone_number):
        await compliance.record_opt_out(session, "+919876500000", business.id)

        with pytest.raises(ComplianceError) as exc:
            await call_service.initiate_call(
                session,
                business.id,
                InitiateCallRequest(agent_id=agent.id, to_number="9876500000"),
            )

        assert exc.value.details["code"] == "dnd_blocked"
        # The blocked attempt is still persisted, for the compliance audit trail.
        blocked = await session.get(CallLog, uuid.UUID(exc.value.details["call_id"]))
        assert blocked is not None
        assert blocked.status == CallStatus.BLOCKED_DND
        assert blocked.ended_at is not None

    async def test_call_outside_calling_hours_is_blocked(
        self, session, business, agent, phone_number
    ):
        midnight = datetime.now(IST).replace(hour=23, minute=30) + timedelta(days=1)

        with pytest.raises(ComplianceError) as exc:
            await call_service.initiate_call(
                session,
                business.id,
                InitiateCallRequest(
                    agent_id=agent.id, to_number="9876500000", scheduled_at=midnight
                ),
            )

        assert exc.value.details["code"] == "outside_calling_hours"
        blocked = await session.get(CallLog, uuid.UUID(exc.value.details["call_id"]))
        assert blocked.status == CallStatus.BLOCKED_HOURS

    async def test_scheduled_call_is_queued_not_dialled(
        self, session, business, agent, phone_number, mock_telephony
    ):
        call = await call_service.initiate_call(
            session,
            business.id,
            InitiateCallRequest(agent_id=agent.id, to_number="9876500000", scheduled_at=in_hours()),
        )

        assert call.status == CallStatus.QUEUED
        assert call.provider_call_id is None
        assert call.scheduled_at is not None

    async def test_variables_seed_the_conversation_state(
        self, session, business, agent, phone_number
    ):
        call = await call_service.initiate_call(
            session,
            business.id,
            InitiateCallRequest(
                agent_id=agent.id,
                to_number="9876500000",
                variables={"invoice_no": "INV-42"},
                scheduled_at=in_hours(),
            ),
        )
        assert call.metadata_json["state"]["variables"] == {"invoice_no": "INV-42"}


class TestInboundRouting:
    async def test_routes_to_the_business_owning_the_dialled_number(
        self, session, business, agent, phone_number
    ):
        call, routed_agent = await call_service.handle_inbound_call(
            session,
            to_number=phone_number.number,
            from_number="+919999988888",
            provider_call_id="prov-in-1",
            provider=TelephonyProvider.MOCK,
        )

        assert call.business_id == business.id
        assert call.direction == CallDirection.INBOUND
        assert call.status == CallStatus.RINGING
        assert routed_agent is not None and routed_agent.id == agent.id

    async def test_unknown_number_is_rejected(self, session):
        from app.core.errors import NotFoundError

        with pytest.raises(NotFoundError, match="No active VoiceDesk number"):
            await call_service.handle_inbound_call(
                session,
                to_number="+910000000000",
                from_number="+919999988888",
                provider_call_id="prov-in-2",
                provider=TelephonyProvider.MOCK,
            )

    async def test_inactive_number_does_not_accept_calls(self, session, phone_number):
        from app.core.errors import NotFoundError

        phone_number.status = PhoneNumberStatus.RELEASED
        await session.flush()

        with pytest.raises(NotFoundError):
            await call_service.handle_inbound_call(
                session,
                to_number=phone_number.number,
                from_number="+919999988888",
                provider_call_id="prov-in-3",
                provider=TelephonyProvider.MOCK,
            )

    async def test_redelivered_ring_returns_the_same_call(self, session, phone_number):
        """Providers retry the ring webhook; it must not create duplicate calls."""
        kwargs = {
            "to_number": phone_number.number,
            "from_number": "+919999988888",
            "provider_call_id": "prov-in-4",
            "provider": TelephonyProvider.MOCK,
        }
        first, _ = await call_service.handle_inbound_call(session, **kwargs)
        second, _ = await call_service.handle_inbound_call(session, **kwargs)
        assert first.id == second.id


class TestHangup:
    async def test_marks_the_call_completed_and_meters_it(self, session, business, call):
        call.answered_at = datetime.now(UTC) - timedelta(seconds=95)
        await session.flush()

        ended = await call_service.hangup_call(session, business.id, call.id, reason="operator")

        assert ended.status == CallStatus.COMPLETED
        assert ended.ended_at is not None
        assert ended.duration_sec >= 90
        # 95s rounds up to 2 billable minutes.
        assert ended.billable_minutes == 2

    async def test_cannot_hang_up_a_terminal_call(self, session, business, call):
        call.status = CallStatus.COMPLETED
        await session.flush()

        with pytest.raises(ConflictError, match="already"):
            await call_service.hangup_call(session, business.id, call.id)

    async def test_unanswered_call_is_marked_unresolved(self, session, business, call):
        ended = await call_service.hangup_call(session, business.id, call.id)
        assert ended.resolution == CallResolution.UNRESOLVED


class TestSoftDelete:
    async def test_sets_deleted_at_rather_than_removing_the_row(self, session, business, call):
        call.status = CallStatus.COMPLETED
        await session.flush()

        await call_service.soft_delete_call(session, business.id, call.id)

        assert (await session.get(CallLog, call.id)) is not None
        assert (await session.get(CallLog, call.id)).deleted_at is not None

    async def test_refuses_to_delete_a_live_call(self, session, business, call):
        with pytest.raises(ConflictError, match="still in progress"):
            await call_service.soft_delete_call(session, business.id, call.id)

    async def test_deleted_call_disappears_from_listings(self, session, business, call):
        call.status = CallStatus.COMPLETED
        await session.flush()
        await call_service.soft_delete_call(session, business.id, call.id)

        calls, total = await call_service.list_calls(session, business.id)
        assert total == 0 and calls == []


class TestListCalls:
    @pytest.fixture(autouse=True)
    async def _seed(self, session, business, agent, phone_number):
        base = datetime.now(UTC)
        rows = [
            CallLog(
                business_id=business.id,
                agent_id=agent.id,
                direction=CallDirection.INBOUND,
                status=CallStatus.COMPLETED,
                resolution=CallResolution.RESOLVED,
                caller_number="+919111111111",
                callee_number=phone_number.number,
                created_at=base - timedelta(days=1),
            ),
            CallLog(
                business_id=business.id,
                agent_id=agent.id,
                direction=CallDirection.OUTBOUND,
                status=CallStatus.NO_ANSWER,
                resolution=CallResolution.UNRESOLVED,
                caller_number="+919222222222",
                callee_number=phone_number.number,
                created_at=base - timedelta(days=5),
            ),
        ]
        session.add_all(rows)
        await session.flush()

    async def test_returns_newest_first(self, session, business):
        calls, total = await call_service.list_calls(session, business.id)
        assert total == 2
        assert calls[0].caller_number == "+919111111111"

    async def test_filters_by_direction(self, session, business):
        calls, total = await call_service.list_calls(
            session, business.id, direction=CallDirection.OUTBOUND
        )
        assert total == 1 and calls[0].direction == CallDirection.OUTBOUND

    async def test_filters_by_status_and_resolution(self, session, business):
        _, completed = await call_service.list_calls(
            session, business.id, status=CallStatus.COMPLETED
        )
        _, resolved = await call_service.list_calls(
            session, business.id, resolution=CallResolution.RESOLVED
        )
        assert completed == 1 and resolved == 1

    async def test_filters_by_caller_number_fragment(self, session, business):
        _, total = await call_service.list_calls(session, business.id, caller_number="9222")
        assert total == 1

    async def test_filters_by_date_window(self, session, business):
        _, total = await call_service.list_calls(
            session, business.id, date_from=datetime.now(UTC) - timedelta(days=2)
        )
        assert total == 1

    async def test_pagination_reports_the_unpaged_total(self, session, business):
        calls, total = await call_service.list_calls(session, business.id, limit=1)
        assert len(calls) == 1 and total == 2


class TestCallEndpoints:
    async def test_initiate_via_http(self, client, owner_headers, agent, phone_number):
        response = await client.post(
            "/api/v1/calls/initiate",
            headers=owner_headers,
            json={"agent_id": str(agent.id), "to_number": "9876500000"},
        )
        assert response.status_code == 201
        assert response.json()["caller_number"] == "+919876500000"

    async def test_compliance_block_surfaces_as_422(
        self, client, session, owner_headers, business, agent, phone_number
    ):
        await compliance.record_opt_out(session, "+919876500000", business.id)

        response = await client.post(
            "/api/v1/calls/initiate",
            headers=owner_headers,
            json={"agent_id": str(agent.id), "to_number": "9876500000"},
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "compliance_blocked"

    async def test_invalid_phone_number_is_rejected(self, client, owner_headers, agent):
        response = await client.post(
            "/api/v1/calls/initiate",
            headers=owner_headers,
            json={"agent_id": str(agent.id), "to_number": "not-a-number"},
        )
        assert response.status_code == 422

    async def test_get_call_includes_transcript_flag(self, client, owner_headers, call):
        response = await client.get(f"/api/v1/calls/{call.id}", headers=owner_headers)
        body = response.json()
        assert response.status_code == 200
        assert body["has_recording"] is False
        assert body["conversations"] == []

    async def test_hangup_via_http(self, client, owner_headers, call):
        response = await client.post(
            f"/api/v1/calls/{call.id}/hangup", headers=owner_headers, json={"reason": "done"}
        )
        assert response.status_code == 200
        assert response.json()["status"] == CallStatus.COMPLETED

    async def test_turn_on_a_terminal_call_is_a_conflict(
        self, client, session, owner_headers, call
    ):
        call.status = CallStatus.COMPLETED
        await session.flush()

        response = await client.post(
            f"/api/v1/calls/{call.id}/turn",
            headers=owner_headers,
            json={"utterance": "hello"},
        )
        assert response.status_code == 409

    async def test_summarise_without_a_transcript_is_404(self, client, owner_headers, call):
        response = await client.post(f"/api/v1/calls/{call.id}/summarise", headers=owner_headers)
        assert response.status_code == 404

    async def test_list_calls_via_http(self, client, owner_headers, call):
        response = await client.get("/api/v1/calls", headers=owner_headers)
        assert response.status_code == 200
        body = response.json()
        assert body["total"] == 1
        assert body["items"][0]["id"] == str(call.id)

    async def test_get_call_reports_a_stored_recording(
        self, client, session, owner_headers, call
    ):
        await recording_service.store_recording(session, call, b"fake-audio-bytes")

        response = await client.get(f"/api/v1/calls/{call.id}", headers=owner_headers)
        assert response.status_code == 200
        assert response.json()["has_recording"] is True

    async def test_get_call_without_transcript_skips_the_engine(
        self, client, owner_headers, call
    ):
        response = await client.get(
            f"/api/v1/calls/{call.id}",
            headers=owner_headers,
            params={"include_transcript": "false"},
        )
        body = response.json()
        assert response.status_code == 200
        assert body["conversations"] == []
        assert body["sentiment_trajectory"] is None

    async def test_transcript_endpoint(self, session, client, owner_headers, call, agent):
        await get_engine().process_turn(session, call, agent, "Namaste")

        response = await client.get(f"/api/v1/calls/{call.id}/transcript", headers=owner_headers)
        assert response.status_code == 200
        turns = response.json()
        assert len(turns) >= 1
        assert turns[0]["role"] == "caller"

    async def test_turn_via_http_returns_the_agent_reply(self, client, owner_headers, call):
        response = await client.post(
            f"/api/v1/calls/{call.id}/turn",
            headers=owner_headers,
            json={"utterance": "Namaste"},
        )
        assert response.status_code == 200
        assert response.json()["awaiting_human"] is False

    async def test_turn_requires_an_assigned_agent(self, client, session, owner_headers, call):
        call.agent_id = None
        await session.flush()

        response = await client.post(
            f"/api/v1/calls/{call.id}/turn",
            headers=owner_headers,
            json={"utterance": "Namaste"},
        )
        assert response.status_code == 409

    async def test_greeting_via_http(self, client, owner_headers, call):
        response = await client.post(f"/api/v1/calls/{call.id}/greeting", headers=owner_headers)
        assert response.status_code == 200
        assert response.json()["reply"] != ""

    async def test_greeting_requires_an_assigned_agent(self, client, session, owner_headers, call):
        call.agent_id = None
        await session.flush()

        response = await client.post(f"/api/v1/calls/{call.id}/greeting", headers=owner_headers)
        assert response.status_code == 409

    async def test_summarise_via_http(self, session, client, owner_headers, call, agent):
        await get_engine().process_turn(session, call, agent, "Namaste")

        response = await client.post(f"/api/v1/calls/{call.id}/summarise", headers=owner_headers)
        assert response.status_code == 200
        assert response.json()["summary"]

    async def test_delete_call_via_http(self, client, session, owner_headers, call):
        call.status = CallStatus.COMPLETED
        await session.flush()

        response = await client.delete(f"/api/v1/calls/{call.id}", headers=owner_headers)
        assert response.status_code == 204

        follow_up = await client.get(f"/api/v1/calls/{call.id}", headers=owner_headers)
        assert follow_up.status_code == 404
