"""Voicemail capture, storage, transcription state and the API."""

from __future__ import annotations

import json
import uuid

import pytest

from app.core.errors import ConflictError, NotFoundError
from app.core.events import EventType
from app.core.security import sign_webhook
from app.models.call import Voicemail
from app.models.enums import CallStatus, VoicemailStatus
from app.services import voicemail_service
from app.services.storage import decrypt_bytes

AUDIO = b"OggS-fake-voicemail-payload" * 8


def signed(payload: dict) -> tuple[bytes, dict[str, str]]:
    """Serialise a webhook body and sign it exactly as a provider would."""
    body = json.dumps(payload).encode()
    return body, {
        "X-VoiceDesk-Signature": sign_webhook(body),
        "Content-Type": "application/json",
    }


class TestStoreVoicemail:
    async def test_stores_encrypted_audio_and_metadata(self, session, business, call, fake_storage):
        voicemail = await voicemail_service.store_voicemail(session, call, AUDIO, duration_sec=17)

        assert voicemail.business_id == business.id
        assert voicemail.call_id == call.id
        assert voicemail.caller_number == call.caller_number
        assert voicemail.status == VoicemailStatus.PENDING
        assert voicemail.duration_sec == 17

        stored = fake_storage.objects[voicemail.storage_path]
        assert AUDIO not in stored
        assert decrypt_bytes(stored) == AUDIO

    async def test_key_is_partitioned_by_tenant_and_distinct_from_recordings(
        self, session, business, call
    ):
        voicemail = await voicemail_service.store_voicemail(session, call, AUDIO)
        assert voicemail.storage_path.startswith(f"voicemails/{business.id}/")

    async def test_empty_payload_is_rejected(self, session, call):
        with pytest.raises(ConflictError, match="empty"):
            await voicemail_service.store_voicemail(session, call, b"")

    async def test_re_ingest_overwrites_rather_than_duplicating(self, session, call):
        first = await voicemail_service.store_voicemail(session, call, AUDIO)
        second = await voicemail_service.store_voicemail(session, call, AUDIO + b"more")

        assert first.id == second.id
        assert second.status == VoicemailStatus.PENDING

    async def test_emits_voicemail_received_event(self, session, call, event_bus):
        voicemail = await voicemail_service.store_voicemail(session, call, AUDIO)

        event = next(e for e in event_bus.published if e.type == EventType.VOICEMAIL_RECEIVED)
        assert event.call_id == call.id
        assert event.data["voicemail_id"] == str(voicemail.id)


class TestIngestFromProvider:
    async def test_downloads_and_re_stores_encrypted(self, session, call, fake_storage):
        voicemail = await voicemail_service.ingest_from_provider_url(
            session, call, "https://provider.test/vm/abc.opus"
        )
        assert voicemail.storage_path in fake_storage.objects


class TestLifecycle:
    async def test_get_voicemail_for_call(self, session, business, call):
        stored = await voicemail_service.store_voicemail(session, call, AUDIO)
        found = await voicemail_service.get_voicemail_for_call(session, business.id, call.id)
        assert found.id == stored.id

    async def test_get_voicemail_for_call_missing_is_404(self, session, business, call):
        with pytest.raises(NotFoundError):
            await voicemail_service.get_voicemail_for_call(session, business.id, call.id)

    async def test_mark_listened_is_idempotent(self, session, business, call, owner):
        voicemail = await voicemail_service.store_voicemail(session, call, AUDIO)

        first = await voicemail_service.mark_listened(session, business.id, voicemail.id, owner.id)
        assert first.listened_at is not None
        assert first.listened_by == owner.id

        second = await voicemail_service.mark_listened(session, business.id, voicemail.id, owner.id)
        assert second.listened_at == first.listened_at

    async def test_set_transcript_marks_transcribed(self, session, business, call):
        voicemail = await voicemail_service.store_voicemail(session, call, AUDIO)

        updated = await voicemail_service.set_transcript(
            session, business.id, voicemail.id, "Please call me back about the invoice."
        )
        assert updated.status == VoicemailStatus.TRANSCRIBED
        assert updated.transcript == "Please call me back about the invoice."
        assert updated.transcribed_at is not None

    async def test_mark_transcription_failed(self, session, business, call):
        voicemail = await voicemail_service.store_voicemail(session, call, AUDIO)
        updated = await voicemail_service.mark_transcription_failed(
            session, business.id, voicemail.id
        )
        assert updated.status == VoicemailStatus.FAILED

    async def test_list_unheard_only(self, session, business, call):
        voicemail = await voicemail_service.store_voicemail(session, call, AUDIO)

        all_vms, total = await voicemail_service.list_voicemails(session, business.id)
        assert total == 1 and len(all_vms) == 1

        await voicemail_service.mark_listened(session, business.id, voicemail.id, uuid.uuid4())
        unheard, unheard_total = await voicemail_service.list_voicemails(
            session, business.id, unheard_only=True
        )
        assert unheard == [] and unheard_total == 0

    async def test_delete_purges_the_audio(self, session, business, call, fake_storage):
        voicemail = await voicemail_service.store_voicemail(session, call, AUDIO)
        await voicemail_service.delete_voicemail(session, business.id, voicemail.id)

        assert fake_storage.objects == {}
        with pytest.raises(NotFoundError):
            await voicemail_service.get_voicemail(session, business.id, voicemail.id)

    async def test_another_tenant_cannot_load_the_audio(self, session, other_business, call):
        await voicemail_service.store_voicemail(session, call, AUDIO)
        with pytest.raises(NotFoundError):
            await voicemail_service.load_audio(session, other_business.id, call.id)


class TestVoicemailEndpoints:
    async def test_list_and_get(self, client, session, owner_headers, call):
        voicemail = await voicemail_service.store_voicemail(session, call, AUDIO)

        listing = await client.get("/api/v1/voicemails", headers=owner_headers)
        assert listing.json()["total"] == 1

        detail = await client.get(f"/api/v1/voicemails/{voicemail.id}", headers=owner_headers)
        assert detail.status_code == 200
        assert detail.json()["status"] == "pending"

    async def test_stream_returns_decrypted_audio(self, client, session, owner_headers, call):
        voicemail = await voicemail_service.store_voicemail(session, call, AUDIO)

        response = await client.get(
            f"/api/v1/voicemails/{voicemail.id}/stream", headers=owner_headers
        )
        assert response.status_code == 200
        assert response.content == AUDIO

    async def test_stream_is_tenant_scoped(self, client, session, other_headers, call):
        voicemail = await voicemail_service.store_voicemail(session, call, AUDIO)
        response = await client.get(
            f"/api/v1/voicemails/{voicemail.id}/stream", headers=other_headers
        )
        assert response.status_code == 404

    async def test_listen_endpoint_stamps_the_operator(self, client, session, owner_headers, call):
        voicemail = await voicemail_service.store_voicemail(session, call, AUDIO)

        response = await client.post(
            f"/api/v1/voicemails/{voicemail.id}/listen", headers=owner_headers
        )
        assert response.status_code == 200
        assert response.json()["listened_at"] is not None

    async def test_viewer_cannot_mark_listened(self, client, session, viewer_headers, call):
        voicemail = await voicemail_service.store_voicemail(session, call, AUDIO)
        response = await client.post(
            f"/api/v1/voicemails/{voicemail.id}/listen", headers=viewer_headers
        )
        assert response.status_code == 403

    async def test_transcript_endpoint(self, client, session, owner_headers, call):
        voicemail = await voicemail_service.store_voicemail(session, call, AUDIO)

        response = await client.post(
            f"/api/v1/voicemails/{voicemail.id}/transcript",
            headers=owner_headers,
            json={"transcript": "Call me back please."},
        )
        assert response.status_code == 200
        assert response.json()["status"] == "transcribed"

    async def test_delete_endpoint(self, client, session, owner_headers, call, fake_storage):
        voicemail = await voicemail_service.store_voicemail(session, call, AUDIO)

        response = await client.delete(f"/api/v1/voicemails/{voicemail.id}", headers=owner_headers)
        assert response.status_code == 204
        assert fake_storage.objects == {}

    async def test_list_is_tenant_scoped(
        self, client, session, owner_headers, call, other_business
    ):
        await voicemail_service.store_voicemail(session, call, AUDIO)
        session.add(
            Voicemail(
                business_id=other_business.id,
                call_id=uuid.uuid4(),
                caller_number="+919999900000",
                storage_path="voicemails/other/x.opus.enc",
                storage_bucket="test",
            )
        )
        await session.flush()

        response = await client.get("/api/v1/voicemails", headers=owner_headers)
        assert len(response.json()["items"]) == 1


class TestWebhookVoicemailIngestion:
    async def test_no_answer_with_voicemail_flag_stores_a_voicemail_not_a_recording(
        self, client, session, business, call
    ):
        call.status = CallStatus.RINGING
        await session.flush()

        body, headers = signed(
            {
                "provider": "mock",
                "provider_call_id": call.provider_call_id,
                "call_id": str(call.id),
                "status": "no-answer",
                "recording_url": "https://provider.test/vm/final.opus",
                "voicemail": True,
            }
        )
        response = await client.post(
            "/api/v1/webhooks/telephony", content=body, headers=headers
        )
        assert response.status_code == 200

        voicemail = await voicemail_service.get_voicemail_for_call(session, business.id, call.id)
        assert voicemail is not None

        with pytest.raises(NotFoundError):
            from app.services import recording_service

            await recording_service.get_recording(session, business.id, call.id)
