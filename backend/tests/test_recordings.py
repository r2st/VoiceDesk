"""Call recording storage, encryption and retention (design doc §2.4, §8.1)."""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.core.errors import ConflictError, NotFoundError
from app.models.call import CallRecording
from app.services import recording_service
from app.services.storage import decrypt_bytes, encrypt_bytes

AUDIO = b"OggS-fake-opus-payload-for-tests" * 8


class TestEncryption:
    def test_round_trip(self):
        assert decrypt_bytes(encrypt_bytes(AUDIO)) == AUDIO

    def test_ciphertext_does_not_contain_the_plaintext(self):
        assert AUDIO not in encrypt_bytes(AUDIO)

    def test_each_encryption_uses_a_fresh_nonce(self):
        """A reused GCM nonce would be a key-recovery vulnerability."""
        assert encrypt_bytes(AUDIO) != encrypt_bytes(AUDIO)

    def test_truncated_ciphertext_is_rejected(self):
        with pytest.raises(ValueError, match="too short"):
            decrypt_bytes(b"tiny")

    def test_tampered_ciphertext_fails_the_auth_tag(self):
        from cryptography.exceptions import InvalidTag

        payload = bytearray(encrypt_bytes(AUDIO))
        payload[-1] ^= 0xFF
        # Specifically InvalidTag: the point is that GCM authenticated the
        # ciphertext and rejected it, not merely that decryption errored.
        with pytest.raises(InvalidTag):
            decrypt_bytes(bytes(payload))


class TestStoreRecording:
    async def test_stores_encrypted_audio_and_metadata(self, session, business, call, fake_storage):
        recording = await recording_service.store_recording(session, call, AUDIO, duration_sec=42)

        assert recording.business_id == business.id
        assert recording.encrypted is True
        assert recording.encryption_algorithm == "AES-256-GCM"
        assert recording.duration_sec == 42
        assert recording.checksum_sha256 == hashlib.sha256(AUDIO).hexdigest()

        # What actually landed in the bucket must not be the plaintext.
        stored = fake_storage.objects[recording.storage_path]
        assert AUDIO not in stored
        assert decrypt_bytes(stored) == AUDIO

    async def test_key_is_partitioned_by_tenant(self, session, business, call):
        recording = await recording_service.store_recording(session, call, AUDIO)
        assert recording.storage_path.startswith(f"recordings/{business.id}/")

    async def test_empty_payload_is_rejected(self, session, call):
        with pytest.raises(ConflictError, match="empty"):
            await recording_service.store_recording(session, call, b"")

    async def test_re_ingest_overwrites_rather_than_duplicating(self, session, business, call):
        first = await recording_service.store_recording(session, call, AUDIO)
        second = await recording_service.store_recording(session, call, AUDIO + b"more")

        assert first.id == second.id
        assert second.checksum_sha256 == hashlib.sha256(AUDIO + b"more").hexdigest()

    async def test_expiry_follows_the_default_retention(self, session, call):
        recording = await recording_service.store_recording(session, call, AUDIO)
        days = (recording.expires_at - datetime.now(UTC)).days
        assert 88 <= days <= 90

    async def test_business_can_override_retention(self, session, business, call):
        business.settings_json = {"recording_retention_days": 30}
        await session.flush()

        recording = await recording_service.store_recording(session, call, AUDIO)
        assert (recording.expires_at - datetime.now(UTC)).days <= 30

    async def test_invalid_retention_setting_falls_back_to_the_default(
        self, session, business, call
    ):
        business.settings_json = {"recording_retention_days": "not-a-number"}
        await session.flush()

        recording = await recording_service.store_recording(session, call, AUDIO)
        assert (recording.expires_at - datetime.now(UTC)).days >= 88


class TestLoadAudio:
    async def test_decrypts_and_verifies_the_checksum(self, session, business, call):
        await recording_service.store_recording(session, call, AUDIO)

        audio, recording = await recording_service.load_audio(session, business.id, call.id)
        assert audio == AUDIO
        assert recording.call_id == call.id

    async def test_corrupted_object_fails_the_integrity_check(
        self, session, business, call, fake_storage
    ):
        recording = await recording_service.store_recording(session, call, AUDIO)
        # Replace the stored object with valid ciphertext of different audio.
        fake_storage.objects[recording.storage_path] = encrypt_bytes(b"different audio")

        with pytest.raises(ConflictError, match="integrity check"):
            await recording_service.load_audio(session, business.id, call.id)

    async def test_missing_recording_is_a_404(self, session, business, call):
        with pytest.raises(NotFoundError, match="No recording"):
            await recording_service.get_recording(session, business.id, call.id)

    async def test_another_tenant_cannot_load_the_audio(
        self, session, business, other_business, call
    ):
        await recording_service.store_recording(session, call, AUDIO)

        with pytest.raises(NotFoundError):
            await recording_service.load_audio(session, other_business.id, call.id)


class TestRetention:
    async def test_purge_expired_removes_the_audio_but_keeps_the_row(
        self, session, business, call, fake_storage
    ):
        recording = await recording_service.store_recording(session, call, AUDIO)
        recording.expires_at = datetime.now(UTC) - timedelta(days=1)
        await session.flush()

        purged = await recording_service.purge_expired(session)

        assert purged == 1
        assert fake_storage.objects == {}, "the audio object is gone"

        row = await session.get(CallRecording, recording.id)
        assert row is not None, "the audit trail that a recording existed is kept"
        assert row.purged_at is not None and row.deleted_at is not None

    async def test_unexpired_recordings_are_untouched(self, session, call, fake_storage):
        await recording_service.store_recording(session, call, AUDIO)
        assert await recording_service.purge_expired(session) == 0
        assert len(fake_storage.objects) == 1

    async def test_purge_is_not_repeated_on_a_second_run(self, session, call):
        recording = await recording_service.store_recording(session, call, AUDIO)
        recording.expires_at = datetime.now(UTC) - timedelta(days=1)
        await session.flush()

        assert await recording_service.purge_expired(session) == 1
        assert await recording_service.purge_expired(session) == 0

    async def test_purged_recording_is_no_longer_retrievable(self, session, business, call):
        recording = await recording_service.store_recording(session, call, AUDIO)
        recording.expires_at = datetime.now(UTC) - timedelta(days=1)
        await session.flush()
        await recording_service.purge_expired(session)

        with pytest.raises(NotFoundError, match="purged"):
            await recording_service.get_recording(session, business.id, call.id)

    async def test_single_purge_ahead_of_schedule(self, session, business, call):
        recording = await recording_service.store_recording(session, call, AUDIO)

        assert await recording_service.purge_recording(session, recording) is True
        assert await recording_service.purge_recording(session, recording) is False


class TestIngestFromProvider:
    async def test_downloads_and_re_stores_encrypted(self, session, business, call, fake_storage):
        recording = await recording_service.ingest_from_provider_url(
            session, call, "https://provider.test/rec/xyz.opus"
        )

        assert recording.encrypted is True
        assert recording.storage_path in fake_storage.objects


class TestRecordingEndpoints:
    async def test_metadata_endpoint(self, client, session, owner_headers, call):
        await recording_service.store_recording(session, call, AUDIO)

        response = await client.get(f"/api/v1/recordings/{call.id}", headers=owner_headers)
        assert response.status_code == 200
        assert response.json()["encrypted"] is True

    async def test_stream_returns_decrypted_audio(self, client, session, owner_headers, call):
        await recording_service.store_recording(session, call, AUDIO)

        response = await client.get(f"/api/v1/recordings/{call.id}/stream", headers=owner_headers)
        assert response.status_code == 200
        assert response.content == AUDIO
        assert response.headers["cache-control"] == "private, no-store"

    async def test_stream_is_tenant_scoped(self, client, session, other_headers, call):
        await recording_service.store_recording(session, call, AUDIO)

        response = await client.get(f"/api/v1/recordings/{call.id}/stream", headers=other_headers)
        assert response.status_code == 404

    async def test_missing_recording_is_404(self, client, owner_headers, call):
        response = await client.get(f"/api/v1/recordings/{call.id}", headers=owner_headers)
        assert response.status_code == 404

    async def test_presigned_url_warns_that_the_object_is_ciphertext(
        self, client, session, owner_headers, call
    ):
        await recording_service.store_recording(session, call, AUDIO)

        response = await client.get(f"/api/v1/recordings/{call.id}/url", headers=owner_headers)
        body = response.json()
        assert body["url"].startswith("https://")
        assert "ciphertext" in body["note"]

    async def test_delete_purges_the_audio(
        self, client, session, owner_headers, call, fake_storage
    ):
        await recording_service.store_recording(session, call, AUDIO)

        response = await client.delete(f"/api/v1/recordings/{call.id}", headers=owner_headers)
        assert response.status_code == 204
        assert fake_storage.objects == {}

    async def test_list_is_tenant_scoped(
        self, client, session, owner_headers, call, other_business
    ):
        await recording_service.store_recording(session, call, AUDIO)
        session.add(
            CallRecording(
                business_id=other_business.id,
                call_id=uuid.uuid4(),
                storage_path="recordings/other/x.opus.enc",
                storage_bucket="test",
            )
        )
        await session.flush()

        response = await client.get("/api/v1/recordings", headers=owner_headers)
        assert len(response.json()["items"]) == 1
