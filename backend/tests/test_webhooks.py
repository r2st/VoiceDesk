"""Telephony webhook ingestion: signatures, idempotency and routing (§5.1)."""

from __future__ import annotations

import json

from app.core.security import sign_webhook
from app.models.enums import CallResolution, CallStatus, TelephonyProvider
from app.services import call_service
from app.services.telephony import WebhookEvent

TELEPHONY_URL = "/api/v1/webhooks/telephony"
STATUS_URL = "/api/v1/webhooks/call-status"


def signed(payload: dict) -> tuple[bytes, dict[str, str]]:
    """Serialise a webhook body and sign it exactly as a provider would."""
    body = json.dumps(payload).encode()
    return body, {
        "X-VoiceDesk-Signature": sign_webhook(body),
        "Content-Type": "application/json",
    }


class TestSignatureVerification:
    async def test_valid_signature_is_accepted(self, client, call):
        body, headers = signed({"provider": "mock", "call_id": str(call.id), "status": "completed"})
        response = await client.post(TELEPHONY_URL, content=body, headers=headers)
        assert response.status_code == 200

    async def test_missing_signature_is_rejected(self, client, call):
        response = await client.post(
            TELEPHONY_URL, json={"call_id": str(call.id), "status": "completed"}
        )
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "authentication_error"

    async def test_wrong_signature_is_rejected(self, client, call):
        body, _ = signed({"call_id": str(call.id), "status": "completed"})
        response = await client.post(
            TELEPHONY_URL,
            content=body,
            headers={"X-VoiceDesk-Signature": "deadbeef", "Content-Type": "application/json"},
        )
        assert response.status_code == 401

    async def test_signature_over_a_tampered_body_is_rejected(self, client, call):
        _, headers = signed({"call_id": str(call.id), "status": "completed"})
        tampered = json.dumps({"call_id": str(call.id), "status": "failed"}).encode()

        response = await client.post(TELEPHONY_URL, content=tampered, headers=headers)
        assert response.status_code == 401

    async def test_provider_specific_signature_header_is_accepted(self, client, call):
        body = json.dumps({"call_id": str(call.id), "status": "completed"}).encode()
        response = await client.post(
            TELEPHONY_URL,
            content=body,
            headers={
                "X-Exotel-Signature": sign_webhook(body),
                "Content-Type": "application/json",
            },
        )
        assert response.status_code == 200

    async def test_malformed_json_is_a_422_not_a_500(self, client):
        body = b"{not json"
        response = await client.post(
            TELEPHONY_URL,
            content=body,
            headers={
                "X-VoiceDesk-Signature": sign_webhook(body),
                "Content-Type": "application/json",
            },
        )
        assert response.status_code == 422

    async def test_json_array_body_is_rejected(self, client):
        body = json.dumps([1, 2, 3]).encode()
        response = await client.post(
            TELEPHONY_URL,
            content=body,
            headers={
                "X-VoiceDesk-Signature": sign_webhook(body),
                "Content-Type": "application/json",
            },
        )
        assert response.status_code == 422


class TestStatusTransitions:
    async def test_completed_event_ends_the_call_and_meters_it(self, client, session, call):
        body, headers = signed(
            {
                "provider": "mock",
                "call_id": str(call.id),
                "status": "completed",
                "duration_sec": 125,
            }
        )
        response = await client.post(TELEPHONY_URL, content=body, headers=headers)

        assert response.json()["status"] == CallStatus.COMPLETED
        await session.refresh(call)
        assert call.status == CallStatus.COMPLETED
        assert call.duration_sec == 125
        assert call.billable_minutes == 3
        assert call.ended_at is not None

    async def test_answer_event_stamps_answered_at(self, client, session, call):
        call.status = CallStatus.RINGING
        call.answered_at = None
        await session.flush()

        body, headers = signed(
            {"provider": "mock", "call_id": str(call.id), "status": "in-progress"}
        )
        await client.post(TELEPHONY_URL, content=body, headers=headers)

        await session.refresh(call)
        assert call.status == CallStatus.IN_PROGRESS
        assert call.answered_at is not None

    async def test_no_answer_marks_the_call_unresolved(self, client, session, call):
        body, headers = signed({"provider": "mock", "call_id": str(call.id), "status": "no-answer"})
        await client.post(TELEPHONY_URL, content=body, headers=headers)

        await session.refresh(call)
        assert call.status == CallStatus.NO_ANSWER
        assert call.resolution == CallResolution.UNRESOLVED

    async def test_error_fields_are_recorded(self, client, session, call):
        body, headers = signed(
            {
                "provider": "mock",
                "call_id": str(call.id),
                "status": "failed",
                "error_code": "NETWORK",
                "error_message": "carrier rejected",
            }
        )
        await client.post(TELEPHONY_URL, content=body, headers=headers)

        await session.refresh(call)
        assert call.error_code == "NETWORK"
        assert call.error_message == "carrier rejected"


class TestIdempotency:
    async def test_replayed_terminal_event_changes_nothing(self, client, session, call):
        body, headers = signed(
            {
                "provider": "mock",
                "call_id": str(call.id),
                "status": "completed",
                "duration_sec": 125,
            }
        )
        first = await client.post(TELEPHONY_URL, content=body, headers=headers)
        second = await client.post(TELEPHONY_URL, content=body, headers=headers)

        assert first.json()["duplicate"] is False
        assert second.json()["duplicate"] is True

        await session.refresh(call)
        assert call.duration_sec == 125
        assert call.billable_minutes == 3, "a replay must not double-charge"

    async def test_a_call_never_moves_out_of_a_terminal_state(self, client, session, call):
        """A late 'ringing' after 'completed' must not resurrect the call."""
        completed, headers = signed(
            {"provider": "mock", "call_id": str(call.id), "status": "completed"}
        )
        await client.post(TELEPHONY_URL, content=completed, headers=headers)

        late, late_headers = signed(
            {"provider": "mock", "call_id": str(call.id), "status": "ringing"}
        )
        response = await client.post(TELEPHONY_URL, content=late, headers=late_headers)

        assert response.json()["duplicate"] is True
        await session.refresh(call)
        assert call.status == CallStatus.COMPLETED

    async def test_unknown_call_is_acknowledged_not_errored(self, client):
        """Acknowledging stops the provider retrying an event we cannot place."""
        body, headers = signed(
            {"provider": "mock", "provider_call_id": "never-seen", "status": "completed"}
        )
        response = await client.post(TELEPHONY_URL, content=body, headers=headers)

        assert response.status_code == 200
        assert response.json() == {
            "received": True,
            "call_id": None,
            "status": None,
            "duplicate": False,
        }


class TestCallLookup:
    async def test_matches_by_provider_call_id_when_our_id_is_absent(self, client, session, call):
        body, headers = signed(
            {
                "provider": "mock",
                "provider_call_id": call.provider_call_id,
                "status": "completed",
            }
        )
        response = await client.post(TELEPHONY_URL, content=body, headers=headers)

        assert response.json()["call_id"] == str(call.id)

    async def test_provider_call_id_is_backfilled(self, client, session, call):
        call.provider_call_id = None
        await session.flush()

        body, headers = signed(
            {
                "provider": "mock",
                "call_id": str(call.id),
                "provider_call_id": "late-id-1",
                "status": "completed",
            }
        )
        await client.post(TELEPHONY_URL, content=body, headers=headers)

        await session.refresh(call)
        assert call.provider_call_id == "late-id-1"


class TestInboundRouting:
    async def test_incoming_ring_creates_a_call(self, client, session, phone_number):
        body, headers = signed(
            {
                "provider": "mock",
                "direction": "incoming",
                "status": "ringing",
                "provider_call_id": "inbound-1",
                "to": phone_number.number,
                "from": "+919999977777",
            }
        )
        response = await client.post(TELEPHONY_URL, content=body, headers=headers)

        assert response.status_code == 200
        assert response.json()["call_id"] is not None
        assert response.json()["status"] == CallStatus.RINGING

    async def test_replayed_inbound_ring_does_not_duplicate_the_call(
        self, client, session, phone_number
    ):
        body, headers = signed(
            {
                "provider": "mock",
                "direction": "incoming",
                "status": "ringing",
                "provider_call_id": "inbound-2",
                "to": phone_number.number,
                "from": "+919999977777",
            }
        )
        first = await client.post(TELEPHONY_URL, content=body, headers=headers)
        second = await client.post(TELEPHONY_URL, content=body, headers=headers)

        assert first.json()["call_id"] == second.json()["call_id"]

    async def test_inbound_to_an_unknown_number_is_a_404(self, client):
        body, headers = signed(
            {
                "provider": "mock",
                "direction": "incoming",
                "status": "ringing",
                "provider_call_id": "inbound-3",
                "to": "+910000000000",
                "from": "+919999977777",
            }
        )
        response = await client.post(TELEPHONY_URL, content=body, headers=headers)
        assert response.status_code == 404


class TestRecordingIngest:
    async def test_recording_url_is_pulled_into_our_bucket(
        self, client, session, call, fake_storage
    ):
        body, headers = signed(
            {
                "provider": "mock",
                "call_id": str(call.id),
                "status": "completed",
                "duration_sec": 60,
                "recording_url": "https://provider.test/rec/abc.opus",
            }
        )
        response = await client.post(TELEPHONY_URL, content=body, headers=headers)

        assert response.status_code == 200
        assert fake_storage.objects, "the audio must be copied into our own storage"

    async def test_a_failing_ingest_does_not_fail_the_webhook(
        self, client, session, call, fake_storage
    ):
        """The call status must still be recorded even if the audio copy fails."""
        fake_storage.fail_upload = True

        body, headers = signed(
            {
                "provider": "mock",
                "call_id": str(call.id),
                "status": "completed",
                "duration_sec": 60,
                "recording_url": "https://provider.test/rec/abc.opus",
            }
        )
        response = await client.post(TELEPHONY_URL, content=body, headers=headers)

        assert response.status_code == 200
        await session.refresh(call)
        assert call.status == CallStatus.COMPLETED


class TestCallStatusEndpoint:
    async def test_applies_a_status_transition(self, client, session, call):
        body, headers = signed({"provider": "mock", "call_id": str(call.id), "status": "completed"})
        response = await client.post(STATUS_URL, content=body, headers=headers)

        assert response.json()["status"] == CallStatus.COMPLETED

    async def test_rejects_an_unsigned_body(self, client, call):
        response = await client.post(STATUS_URL, json={"status": "completed"})
        assert response.status_code == 401

    async def test_ignores_a_recording_url(self, client, session, call, fake_storage):
        """The status-only sink must not perform side effects beyond the status."""
        body, headers = signed(
            {
                "provider": "mock",
                "call_id": str(call.id),
                "status": "completed",
                "recording_url": "https://provider.test/rec/abc.opus",
            }
        )
        await client.post(STATUS_URL, content=body, headers=headers)
        assert fake_storage.objects == {}


class TestFormEncodedBodies:
    async def test_exotel_style_form_post_is_parsed(self, client, session, call):
        """Exotel posts application/x-www-form-urlencoded, not JSON."""
        body = f"CallSid={call.provider_call_id}&Status=completed&ConversationDuration=90".encode()
        response = await client.post(
            f"{TELEPHONY_URL}?provider=exotel",
            content=body,
            headers={
                "X-VoiceDesk-Signature": sign_webhook(body),
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        # The call was created against the mock provider, so exotel cannot match
        # it — the point is that the body parsed and produced a clean 200.
        assert response.status_code == 200


class TestApplyWebhookEventUnit:
    async def test_derives_duration_when_the_provider_omits_it(self, session, call):
        from datetime import UTC, datetime, timedelta

        call.answered_at = datetime.now(UTC) - timedelta(seconds=42)
        await session.flush()

        updated, duplicate = await call_service.apply_webhook_event(
            session,
            WebhookEvent(
                provider_call_id=call.provider_call_id or "",
                status=CallStatus.COMPLETED,
                call_id=str(call.id),
                duration_sec=0,
            ),
            TelephonyProvider.MOCK,
        )

        assert duplicate is False
        assert updated is not None and updated.duration_sec >= 40

    async def test_returns_none_for_an_unplaceable_event(self, session):
        call, duplicate = await call_service.apply_webhook_event(
            session,
            WebhookEvent(provider_call_id="nope", status=CallStatus.COMPLETED),
            TelephonyProvider.MOCK,
        )
        assert call is None and duplicate is False
