"""Call recording storage: encryption and the S3 wrapper (design doc §2.4).

Every other test file exercises this through `FakeStorage` (tests/fakes.py) so
the encryption path is real but boto3 never is. This file is the reverse: a
fake S3 client stands in for boto3 so `StorageService` itself — the class the
fake is standing in for everywhere else — gets covered directly, including
the `ClientError`/`BotoCoreError` handling that only lives here.
"""

from __future__ import annotations

import base64
import hashlib

import pytest
from botocore.exceptions import ClientError
from cryptography.exceptions import InvalidTag

from app.core.errors import ExternalServiceError, NotFoundError
from app.services.storage import (
    StorageService,
    StoredObject,
    _encryption_key,
    decrypt_bytes,
    encrypt_bytes,
    get_storage,
    set_storage,
)


def client_error(operation: str, code: str = "NoSuchKey") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "boom"}}, operation)


class _Stream:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body


class FakeS3Client:
    """Just enough of the boto3 S3 surface for `StorageService`."""

    def __init__(self) -> None:
        self.objects: dict[str, dict] = {}
        self.bucket_exists = True
        self.fail_head_bucket = False
        self.fail_create_bucket = False
        self.fail_put = False
        self.fail_get = False
        self.fail_presign = False

    def head_bucket(self, Bucket):  # noqa: ARG002
        if not self.bucket_exists or self.fail_head_bucket:
            raise client_error("HeadBucket", "404")

    def create_bucket(self, Bucket):  # noqa: ARG002
        if self.fail_create_bucket:
            raise client_error("CreateBucket", "BucketAlreadyExists")
        self.bucket_exists = True

    def put_object(self, Bucket, Key, Body, ContentType, Metadata):  # noqa: ARG002
        if self.fail_put:
            raise client_error("PutObject")
        self.objects[Key] = {"Body": Body, "ContentType": ContentType, "Metadata": Metadata}

    def get_object(self, Bucket, Key):  # noqa: ARG002
        if self.fail_get or Key not in self.objects:
            raise client_error("GetObject")
        return {"Body": _Stream(self.objects[Key]["Body"])}

    def generate_presigned_url(self, operation, Params, ExpiresIn):
        if self.fail_presign:
            raise client_error(operation)
        return f"https://s3.test/{Params['Bucket']}/{Params['Key']}?exp={ExpiresIn}"

    def delete_object(self, Bucket, Key):  # noqa: ARG002
        self.objects.pop(Key, None)


def service(**kwargs) -> tuple[StorageService, FakeS3Client]:
    fake = FakeS3Client()
    return StorageService(client=fake, bucket="test-bucket", **kwargs), fake


# --------------------------------------------------------------------------- #
# Encryption
# --------------------------------------------------------------------------- #
class TestEncryption:
    def test_a_roundtrip_recovers_the_plaintext(self):
        assert decrypt_bytes(encrypt_bytes(b"hello world")) == b"hello world"

    def test_each_encryption_uses_a_fresh_nonce(self):
        first = encrypt_bytes(b"same plaintext")
        second = encrypt_bytes(b"same plaintext")
        assert first != second

    def test_tampered_ciphertext_fails_to_decrypt(self):
        payload = bytearray(encrypt_bytes(b"hello world"))
        payload[-1] ^= 0xFF
        with pytest.raises(InvalidTag):
            decrypt_bytes(bytes(payload))

    def test_a_payload_too_short_to_hold_a_nonce_is_rejected(self):
        with pytest.raises(ValueError):
            decrypt_bytes(b"short")

    def test_a_base64_32_byte_key_is_used_directly(self, monkeypatch):
        from app.core.config import settings

        raw_key = hashlib.sha256(b"a real 32 byte key").digest()
        encoded = base64.b64encode(raw_key).decode()
        monkeypatch.setattr(settings, "recording_encryption_key", encoded, raising=False)
        _encryption_key.cache_clear()
        try:
            assert _encryption_key() == raw_key
        finally:
            _encryption_key.cache_clear()

    def test_a_human_typed_secret_is_stretched_with_sha256(self, monkeypatch):
        from app.core.config import settings

        monkeypatch.setattr(settings, "recording_encryption_key", "not-base64-and-not-32-bytes")
        _encryption_key.cache_clear()
        try:
            assert _encryption_key() == hashlib.sha256(b"not-base64-and-not-32-bytes").digest()
        finally:
            _encryption_key.cache_clear()

    def test_an_unset_key_in_production_is_a_configuration_error(self, monkeypatch):
        from app.core.config import settings

        monkeypatch.setattr(settings, "recording_encryption_key", "")
        monkeypatch.setattr(settings, "voicedesk_env", "production")
        _encryption_key.cache_clear()
        try:
            with pytest.raises(ExternalServiceError):
                _encryption_key()
        finally:
            _encryption_key.cache_clear()

    def test_an_unset_key_outside_production_falls_back_to_a_dev_key(self, monkeypatch):
        from app.core.config import settings

        monkeypatch.setattr(settings, "recording_encryption_key", "")
        monkeypatch.setattr(settings, "voicedesk_env", "development")
        _encryption_key.cache_clear()
        try:
            assert len(_encryption_key()) == 32
        finally:
            _encryption_key.cache_clear()


# --------------------------------------------------------------------------- #
# build_key
# --------------------------------------------------------------------------- #
class TestBuildKey:
    def test_the_key_is_tenant_partitioned(self):
        key = StorageService.build_key("biz-1", "call-1")
        assert key == "recordings/biz-1/call-1.opus.enc"

    def test_the_extension_is_configurable(self):
        key = StorageService.build_key("biz-1", "call-1", "wav")
        assert key == "recordings/biz-1/call-1.wav.enc"


# --------------------------------------------------------------------------- #
# Upload
# --------------------------------------------------------------------------- #
class TestUpload:
    def test_upload_encrypts_by_default(self):
        svc, fake = service()

        result = svc.upload("recordings/a/b.opus.enc", b"audio bytes")

        assert result.encrypted is True
        stored = fake.objects["recordings/a/b.opus.enc"]
        assert decrypt_bytes(stored["Body"]) == b"audio bytes"
        assert stored["Metadata"]["encrypted"] == "true"

    def test_upload_can_skip_encryption(self):
        svc, fake = service()

        result = svc.upload("k", b"raw bytes", encrypt=False)

        assert result.encrypted is False
        assert fake.objects["k"]["Body"] == b"raw bytes"
        assert fake.objects["k"]["Metadata"]["encrypted"] == "false"

    def test_upload_records_the_checksum_and_size(self):
        import hashlib

        svc, _ = service()

        result = svc.upload("k", b"raw bytes", encrypt=False)

        assert result.checksum_sha256 == hashlib.sha256(b"raw bytes").hexdigest()
        assert result.size_bytes == len(b"raw bytes")
        assert result.bucket == "test-bucket"

    def test_upload_merges_caller_metadata(self):
        svc, fake = service()

        svc.upload("k", b"x", metadata={"business_id": "biz-1"})

        assert fake.objects["k"]["Metadata"]["business_id"] == "biz-1"

    def test_upload_failure_is_an_external_service_error(self):
        svc, fake = service()
        fake.fail_put = True

        with pytest.raises(ExternalServiceError):
            svc.upload("k", b"x")

    def test_upload_returns_a_stored_object(self):
        svc, _ = service()

        result = svc.upload("k", b"x")

        assert isinstance(result, StoredObject)


# --------------------------------------------------------------------------- #
# Download
# --------------------------------------------------------------------------- #
class TestDownload:
    def test_download_decrypts_by_default(self):
        svc, _ = service()
        svc.upload("k", b"secret audio")

        assert svc.download("k") == b"secret audio"

    def test_download_can_return_raw_ciphertext(self):
        svc, fake = service()
        svc.upload("k", b"secret audio")

        assert svc.download("k", decrypt=False) == fake.objects["k"]["Body"]

    def test_downloading_a_missing_key_is_a_not_found_error(self):
        svc, _ = service()

        with pytest.raises(NotFoundError):
            svc.download("nope")

    def test_a_client_error_on_get_is_a_not_found_error(self):
        svc, fake = service()
        svc.upload("k", b"x")
        fake.fail_get = True

        with pytest.raises(NotFoundError):
            svc.download("k")

    def test_a_corrupted_body_fails_to_decrypt_as_an_external_service_error(self):
        svc, fake = service()
        fake.objects["k"] = {"Body": b"not encrypted data at all"}

        with pytest.raises(ExternalServiceError):
            svc.download("k")


# --------------------------------------------------------------------------- #
# Presigned URLs and deletion
# --------------------------------------------------------------------------- #
class TestPresignedUrl:
    def test_returns_a_url_scoped_to_the_bucket_and_key(self):
        svc, _ = service()

        url = svc.presigned_url("recordings/a/b.enc", expires_seconds=60)

        assert "test-bucket" in url
        assert "recordings/a/b.enc" in url
        assert "exp=60" in url

    def test_signing_failure_is_an_external_service_error(self):
        svc, fake = service()
        fake.fail_presign = True

        with pytest.raises(ExternalServiceError):
            svc.presigned_url("k")


class TestDelete:
    def test_deleting_an_existing_object_returns_true(self):
        svc, _ = service()
        svc.upload("k", b"x")

        assert svc.delete("k") is True

    def test_delete_is_not_an_error_when_the_object_is_already_gone(self):
        svc, _ = service()

        assert svc.delete("nope") is True

    def test_a_client_error_on_delete_returns_false_without_raising(self):
        svc, _ = service()

        class FailingClient(FakeS3Client):
            def delete_object(self, Bucket, Key):  # noqa: ARG002
                raise client_error("DeleteObject")

        svc._client = FailingClient()
        assert svc.delete("k") is False


class TestEnsureBucket:
    def test_an_existing_bucket_is_left_alone(self):
        svc, fake = service()
        fake.bucket_exists = True

        svc.ensure_bucket()

    def test_a_missing_bucket_is_created(self):
        svc, fake = service()
        fake.bucket_exists = False

        svc.ensure_bucket()

        assert fake.bucket_exists is True

    def test_a_bucket_that_cannot_be_created_is_an_external_service_error(self):
        svc, fake = service()
        fake.bucket_exists = False
        fake.fail_create_bucket = True

        with pytest.raises(ExternalServiceError):
            svc.ensure_bucket()


# --------------------------------------------------------------------------- #
# Lazy client construction and the module-level registry
# --------------------------------------------------------------------------- #
class TestClientConstruction:
    def test_no_injected_client_lazily_builds_a_real_boto3_client(self):
        svc = StorageService(bucket="lazy-bucket")

        client = svc.client

        assert hasattr(client, "put_object")
        assert svc.client is client, "the same client is reused, not rebuilt per call"

    def test_the_default_bucket_comes_from_settings(self):
        from app.core.config import settings

        assert StorageService().bucket == settings.s3_bucket


class TestStorageRegistry:
    def test_the_storage_service_is_a_singleton(self):
        set_storage(None)
        try:
            assert get_storage() is get_storage()
        finally:
            set_storage(None)

    def test_an_injected_service_is_returned(self):
        svc, _ = service()
        set_storage(svc)
        try:
            assert get_storage() is svc
        finally:
            set_storage(None)
