"""Password hashing, JWT issuance/verification and webhook HMAC signatures."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from jose import JWTError, jwt
from passlib.context import CryptContext

from app.core.config import settings
from app.core.errors import AuthenticationError

_pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

TokenType = Literal["access", "refresh"]


# --------------------------------------------------------------------------- #
# Passwords
# --------------------------------------------------------------------------- #
def hash_password(password: str) -> str:
    # bcrypt silently truncates at 72 bytes; reject rather than mislead.
    if len(password.encode()) > 72:
        raise ValueError("Password must be at most 72 bytes.")
    return _pwd_context.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return _pwd_context.verify(password, password_hash)
    except ValueError:
        return False


# --------------------------------------------------------------------------- #
# JWT
# --------------------------------------------------------------------------- #
def _create_token(
    *,
    subject: str,
    business_id: str,
    role: str,
    token_type: TokenType,
    expires_delta: timedelta,
    extra: dict[str, Any] | None = None,
) -> tuple[str, datetime]:
    now = datetime.now(UTC)
    expires_at = now + expires_delta
    payload: dict[str, Any] = {
        "sub": subject,
        "business_id": business_id,
        "role": role,
        "type": token_type,
        "iat": int(now.timestamp()),
        "exp": int(expires_at.timestamp()),
        "jti": secrets.token_urlsafe(16),
    }
    if extra:
        payload.update(extra)
    token = jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    return token, expires_at


def create_access_token(
    *, user_id: uuid.UUID | str, business_id: uuid.UUID | str, role: str
) -> tuple[str, datetime]:
    return _create_token(
        subject=str(user_id),
        business_id=str(business_id),
        role=role,
        token_type="access",
        expires_delta=timedelta(minutes=settings.access_token_ttl_minutes),
    )


def create_refresh_token(
    *, user_id: uuid.UUID | str, business_id: uuid.UUID | str, role: str
) -> tuple[str, datetime]:
    return _create_token(
        subject=str(user_id),
        business_id=str(business_id),
        role=role,
        token_type="refresh",
        expires_delta=timedelta(days=settings.refresh_token_ttl_days),
    )


def decode_token(token: str, *, expected_type: TokenType | None = None) -> dict[str, Any]:
    """Decode and validate a JWT, raising :class:`AuthenticationError` on any problem."""
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except JWTError as exc:
        raise AuthenticationError("Invalid or expired token.") from exc

    if expected_type and payload.get("type") != expected_type:
        raise AuthenticationError(f"Expected a {expected_type} token.")
    if not payload.get("sub") or not payload.get("business_id"):
        raise AuthenticationError("Token is missing required claims.")
    return payload


def hash_token(token: str) -> str:
    """SHA-256 digest used to store refresh tokens without keeping the raw value."""
    return hashlib.sha256(token.encode()).hexdigest()


# --------------------------------------------------------------------------- #
# Webhook signatures (design doc §5.1, §8.3)
# --------------------------------------------------------------------------- #
def sign_webhook(payload: bytes, secret: str | None = None) -> str:
    key = (secret or settings.webhook_hmac_secret).encode()
    return hmac.new(key, payload, hashlib.sha256).hexdigest()


def verify_webhook_signature(
    payload: bytes, signature: str | None, secret: str | None = None
) -> bool:
    """Constant-time comparison; tolerates a ``sha256=`` prefix."""
    if not signature:
        return False
    candidate = signature.split("=", 1)[1] if signature.startswith("sha256=") else signature
    return hmac.compare_digest(sign_webhook(payload, secret), candidate.strip())
