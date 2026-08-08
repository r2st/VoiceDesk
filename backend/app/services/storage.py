"""Call recording storage on S3-compatible object storage (design doc §2.4).

Recordings are encrypted with AES-256-GCM before upload, so the bytes at rest
are unreadable even to the storage operator. Playback is served through
short-lived presigned URLs or streamed via the API after decryption.
"""

from __future__ import annotations

import base64
import hashlib
import os
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.config import settings
from app.core.errors import ExternalServiceError, NotFoundError
from app.core.logging import get_logger

logger = get_logger(__name__)

NONCE_BYTES = 12
KEY_BYTES = 32


@dataclass(frozen=True, slots=True)
class StoredObject:
    bucket: str
    key: str
    size_bytes: int
    checksum_sha256: str
    encrypted: bool


@lru_cache
def _encryption_key() -> bytes:
    """The AES-256 key, derived from ``RECORDING_ENCRYPTION_KEY``.

    A base64 32-byte value is used directly; any other string is stretched with
    SHA-256 so a human-typed secret still yields a valid key.
    """
    configured = settings.recording_encryption_key
    if not configured:
        if settings.is_production:
            raise ExternalServiceError(
                "RECORDING_ENCRYPTION_KEY must be set in production."
            )
        # Development fallback: deterministic key derived from the JWT secret.
        configured = f"dev-recording-key:{settings.jwt_secret}"

    try:
        decoded = base64.b64decode(configured, validate=True)
        if len(decoded) == KEY_BYTES:
            return decoded
    except Exception:
        pass
    return hashlib.sha256(configured.encode()).digest()


def encrypt_bytes(plaintext: bytes) -> bytes:
    """AES-256-GCM. The 12-byte nonce is prefixed to the ciphertext."""
    nonce = os.urandom(NONCE_BYTES)
    ciphertext = AESGCM(_encryption_key()).encrypt(nonce, plaintext, None)
    return nonce + ciphertext


def decrypt_bytes(payload: bytes) -> bytes:
    if len(payload) <= NONCE_BYTES:
        raise ValueError("Ciphertext is too short to contain a nonce.")
    nonce, ciphertext = payload[:NONCE_BYTES], payload[NONCE_BYTES:]
    return AESGCM(_encryption_key()).decrypt(nonce, ciphertext, None)


class StorageService:
    """S3/MinIO wrapper for call recordings."""

    def __init__(self, client: Any | None = None, bucket: str | None = None) -> None:
        self._client = client
        self.bucket = bucket or settings.s3_bucket

    @property
    def client(self):
        if self._client is None:
            self._client = boto3.client(
                "s3",
                endpoint_url=settings.s3_endpoint_url or None,
                aws_access_key_id=settings.s3_access_key,
                aws_secret_access_key=settings.s3_secret_key,
                region_name=settings.s3_region,
                config=Config(signature_version="s3v4", retries={"max_attempts": 3}),
            )
        return self._client

    def ensure_bucket(self) -> None:
        try:
            self.client.head_bucket(Bucket=self.bucket)
        except (ClientError, BotoCoreError):
            try:
                self.client.create_bucket(Bucket=self.bucket)
                logger.info("Created recording bucket %s", self.bucket)
            except (ClientError, BotoCoreError) as exc:
                raise ExternalServiceError(f"Could not create bucket: {exc}") from exc

    @staticmethod
    def build_key(business_id: Any, call_id: Any, extension: str = "opus") -> str:
        """Tenant-partitioned object key, so a prefix policy can scope access."""
        return f"recordings/{business_id}/{call_id}.{extension}.enc"

    def upload(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str = "audio/ogg",
        encrypt: bool = True,
        metadata: dict[str, str] | None = None,
    ) -> StoredObject:
        checksum = hashlib.sha256(data).hexdigest()
        body = encrypt_bytes(data) if encrypt else data
        try:
            self.client.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=body,
                ContentType=content_type,
                Metadata={
                    "sha256": checksum,
                    "encrypted": "true" if encrypt else "false",
                    **(metadata or {}),
                },
            )
        except (ClientError, BotoCoreError) as exc:
            raise ExternalServiceError(f"Recording upload failed: {exc}") from exc

        return StoredObject(
            bucket=self.bucket,
            key=key,
            size_bytes=len(body),
            checksum_sha256=checksum,
            encrypted=encrypt,
        )

    def download(self, key: str, *, decrypt: bool = True) -> bytes:
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=key)
            body = response["Body"].read()
        except (ClientError, BotoCoreError) as exc:
            raise NotFoundError(f"Recording object not found: {key}") from exc
        if not decrypt:
            return body
        try:
            return decrypt_bytes(body)
        except Exception as exc:
            raise ExternalServiceError(f"Recording could not be decrypted: {exc}") from exc

    def presigned_url(self, key: str, *, expires_seconds: int = 900) -> str:
        """A short-lived direct download URL.

        Note: the object is encrypted, so a presigned URL yields ciphertext.
        Use it only for internal tooling; the dashboard streams through the API.
        """
        try:
            return self.client.generate_presigned_url(
                "get_object",
                Params={"Bucket": self.bucket, "Key": key},
                ExpiresIn=expires_seconds,
            )
        except (ClientError, BotoCoreError) as exc:
            raise ExternalServiceError(f"Could not sign recording URL: {exc}") from exc

    def delete(self, key: str) -> bool:
        try:
            self.client.delete_object(Bucket=self.bucket, Key=key)
            return True
        except (ClientError, BotoCoreError) as exc:
            logger.warning("Failed to delete %s: %s", key, exc)
            return False


_service: StorageService | None = None


def get_storage() -> StorageService:
    global _service
    if _service is None:
        _service = StorageService()
    return _service


def set_storage(service: StorageService | None) -> None:
    global _service
    _service = service
