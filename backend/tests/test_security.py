"""Password hashing, JWT lifecycle and webhook signatures (design doc §8.3)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from jose import jwt

from app.core.config import settings
from app.core.errors import AuthenticationError
from app.core.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    hash_token,
    sign_webhook,
    verify_password,
    verify_webhook_signature,
)


class TestPasswords:
    def test_hash_is_salted_and_verifiable(self):
        first = hash_password("Sup3rSecret!")
        second = hash_password("Sup3rSecret!")
        assert first != second, "each hash must carry its own salt"
        assert verify_password("Sup3rSecret!", first)
        assert verify_password("Sup3rSecret!", second)

    def test_wrong_password_rejected(self):
        assert not verify_password("wrong", hash_password("Sup3rSecret!"))

    def test_malformed_hash_returns_false_rather_than_raising(self):
        assert not verify_password("anything", "not-a-bcrypt-hash")

    def test_password_over_bcrypt_limit_is_rejected(self):
        # bcrypt truncates silently at 72 bytes; a longer password must error
        # rather than quietly authenticate on its first 72 bytes.
        with pytest.raises(ValueError, match="72 bytes"):
            hash_password("a" * 73)

    def test_multibyte_password_measured_in_bytes(self):
        # 30 Devanagari characters are well under 72 chars but over 72 bytes.
        with pytest.raises(ValueError):
            hash_password("न" * 30)


class TestTokens:
    def test_access_token_round_trip(self):
        user_id, business_id = uuid.uuid4(), uuid.uuid4()
        token, expires = create_access_token(user_id=user_id, business_id=business_id, role="owner")
        payload = decode_token(token, expected_type="access")

        assert payload["sub"] == str(user_id)
        assert payload["business_id"] == str(business_id)
        assert payload["role"] == "owner"
        assert payload["type"] == "access"
        assert expires > datetime.now(UTC)

    def test_refresh_token_lives_longer_than_access_token(self):
        args = {"user_id": uuid.uuid4(), "business_id": uuid.uuid4(), "role": "owner"}
        _, access_exp = create_access_token(**args)
        _, refresh_exp = create_refresh_token(**args)
        assert refresh_exp > access_exp

    def test_token_type_is_enforced(self):
        token, _ = create_refresh_token(
            user_id=uuid.uuid4(), business_id=uuid.uuid4(), role="owner"
        )
        # A refresh token must not be usable as an access token.
        with pytest.raises(AuthenticationError, match="Expected a access token"):
            decode_token(token, expected_type="access")

    def test_expired_token_rejected(self):
        payload = {
            "sub": str(uuid.uuid4()),
            "business_id": str(uuid.uuid4()),
            "role": "owner",
            "type": "access",
            "exp": int((datetime.now(UTC) - timedelta(minutes=1)).timestamp()),
        }
        token = jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)
        with pytest.raises(AuthenticationError):
            decode_token(token, expected_type="access")

    def test_token_signed_with_another_secret_rejected(self):
        payload = {
            "sub": str(uuid.uuid4()),
            "business_id": str(uuid.uuid4()),
            "type": "access",
            "exp": int((datetime.now(UTC) + timedelta(minutes=5)).timestamp()),
        }
        forged = jwt.encode(payload, "attacker-secret", algorithm="HS256")
        with pytest.raises(AuthenticationError):
            decode_token(forged)

    def test_token_missing_business_claim_rejected(self):
        # business_id is what scopes every query — a token without it is unusable.
        payload = {
            "sub": str(uuid.uuid4()),
            "type": "access",
            "exp": int((datetime.now(UTC) + timedelta(minutes=5)).timestamp()),
        }
        token = jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)
        with pytest.raises(AuthenticationError, match="missing required claims"):
            decode_token(token)

    def test_garbage_token_rejected(self):
        with pytest.raises(AuthenticationError):
            decode_token("not.a.jwt")

    def test_each_token_has_a_unique_jti(self):
        args = {"user_id": uuid.uuid4(), "business_id": uuid.uuid4(), "role": "owner"}
        first, _ = create_access_token(**args)
        second, _ = create_access_token(**args)
        assert decode_token(first)["jti"] != decode_token(second)["jti"]

    def test_hash_token_is_stable_and_hides_the_value(self):
        token = "some-refresh-token"
        digest = hash_token(token)
        assert digest == hash_token(token)
        assert token not in digest
        assert len(digest) == 64


class TestWebhookSignatures:
    def test_sign_and_verify(self):
        body = b'{"event":"call.completed"}'
        assert verify_webhook_signature(body, sign_webhook(body))

    def test_sha256_prefix_is_tolerated(self):
        body = b'{"event":"call.completed"}'
        assert verify_webhook_signature(body, f"sha256={sign_webhook(body)}")

    def test_tampered_body_fails(self):
        signature = sign_webhook(b'{"amount":100}')
        assert not verify_webhook_signature(b'{"amount":900}', signature)

    def test_missing_signature_fails(self):
        assert not verify_webhook_signature(b"{}", None)
        assert not verify_webhook_signature(b"{}", "")

    def test_wrong_secret_fails(self):
        body = b"{}"
        assert not verify_webhook_signature(body, sign_webhook(body, "other-secret"))

    def test_signature_is_whitespace_tolerant(self):
        body = b"{}"
        assert verify_webhook_signature(body, f"  {sign_webhook(body)}  ")
