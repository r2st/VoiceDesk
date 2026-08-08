"""Registration, login, token rotation and user management."""

from __future__ import annotations

import re
import secrets
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import AuthenticationError, ConflictError, NotFoundError, ValidationError
from app.core.logging import get_logger
from app.core.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    hash_token,
    verify_password,
)
from app.core.tenancy import tenant_select
from app.models.business import Business, RefreshToken, User
from app.models.enums import BusinessStatus, UserRole
from app.schemas.auth import LoginRequest, RegisterRequest, TokenPair, UserCreate, UserUpdate

logger = get_logger(__name__)

TRIAL_DAYS = 14


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug[:100] or "business"


async def _unique_slug(session: AsyncSession, base: str) -> str:
    """Append a short random suffix until the slug is free."""
    slug = base
    for _ in range(5):
        existing = await session.execute(select(Business.id).where(Business.slug == slug))
        if existing.scalar_one_or_none() is None:
            return slug
        slug = f"{base[:92]}-{secrets.token_hex(3)}"
    raise ConflictError("Could not allocate a unique business slug.")


async def issue_token_pair(
    session: AsyncSession, user: User, *, user_agent: str | None = None
) -> TokenPair:
    """Mint an access/refresh pair and persist the refresh token's digest."""
    access_token, access_expires = create_access_token(
        user_id=user.id, business_id=user.business_id, role=user.role
    )
    refresh_token, refresh_expires = create_refresh_token(
        user_id=user.id, business_id=user.business_id, role=user.role
    )
    session.add(
        RefreshToken(
            business_id=user.business_id,
            user_id=user.id,
            token_hash=hash_token(refresh_token),
            expires_at=refresh_expires,
            user_agent=(user_agent or "")[:300] or None,
        )
    )
    await session.flush()
    return TokenPair(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=settings.access_token_ttl_minutes * 60,
        expires_at=access_expires,
    )


async def register_business(
    session: AsyncSession, payload: RegisterRequest, *, user_agent: str | None = None
) -> tuple[Business, User, TokenPair]:
    """Create a tenant and its owner. The email must be globally unique."""
    existing = await session.execute(
        select(User.id).where(
            func.lower(User.email) == payload.email.lower(), User.deleted_at.is_(None)
        )
    )
    if existing.scalar_one_or_none() is not None:
        raise ConflictError("An account with this email already exists.")

    business = Business(
        name=payload.business_name,
        slug=await _unique_slug(session, slugify(payload.business_name)),
        phone=payload.business_phone,
        email=payload.email.lower(),
        industry=payload.industry,
        gstin=payload.gstin,
        city=payload.city,
        state=payload.state,
        plan=payload.plan,
        status=BusinessStatus.TRIAL,
        trial_ends_at=datetime.now(UTC) + timedelta(days=TRIAL_DAYS),
        settings_json={},
    )
    session.add(business)
    await session.flush()

    user = User(
        business_id=business.id,
        email=payload.email.lower(),
        full_name=payload.full_name,
        password_hash=hash_password(payload.password),
        role=UserRole.OWNER,
        is_active=True,
    )
    session.add(user)
    await session.flush()

    tokens = await issue_token_pair(session, user, user_agent=user_agent)
    logger.info("Registered business %s (%s)", business.slug, business.id)
    return business, user, tokens


async def authenticate(
    session: AsyncSession, payload: LoginRequest, *, user_agent: str | None = None
) -> tuple[User, TokenPair]:
    stmt = select(User).where(
        func.lower(User.email) == payload.email.lower(), User.deleted_at.is_(None)
    )
    if payload.business_slug:
        stmt = stmt.join(Business, Business.id == User.business_id).where(
            Business.slug == payload.business_slug
        )
    candidates = (await session.execute(stmt)).scalars().all()

    # Always run one hash comparison so a missing user and a wrong password
    # take a comparable amount of time.
    user = next((u for u in candidates if verify_password(payload.password, u.password_hash)), None)
    if user is None:
        if not candidates:
            verify_password(payload.password, hash_password("dummy-password-for-timing"))
        raise AuthenticationError("Invalid email or password.")
    if not user.is_active:
        raise AuthenticationError("This account has been deactivated.")

    business = await session.get(Business, user.business_id)
    if business is None or business.deleted_at is not None:
        raise AuthenticationError("This business account is no longer available.")
    if business.status == BusinessStatus.CANCELLED:
        raise AuthenticationError("This business account has been cancelled.")

    user.last_login_at = datetime.now(UTC)
    tokens = await issue_token_pair(session, user, user_agent=user_agent)
    return user, tokens


async def refresh_tokens(
    session: AsyncSession, refresh_token: str, *, user_agent: str | None = None
) -> TokenPair:
    """Rotate a refresh token: the presented token is revoked as it is consumed."""
    payload = decode_token(refresh_token, expected_type="refresh")
    digest = hash_token(refresh_token)

    record = (
        await session.execute(select(RefreshToken).where(RefreshToken.token_hash == digest))
    ).scalar_one_or_none()
    if record is None:
        raise AuthenticationError("Refresh token is not recognised.")
    if record.revoked_at is not None:
        # Re-use of an already-rotated token: revoke the whole family.
        await revoke_all_for_user(session, record.user_id)
        raise AuthenticationError("Refresh token has already been used.")
    if record.expires_at <= datetime.now(UTC):
        raise AuthenticationError("Refresh token has expired.")

    user = await session.get(User, uuid.UUID(payload["sub"]))
    if user is None or user.deleted_at is not None or not user.is_active:
        raise AuthenticationError("User is no longer active.")

    record.revoked_at = datetime.now(UTC)
    return await issue_token_pair(session, user, user_agent=user_agent)


async def revoke_refresh_token(session: AsyncSession, refresh_token: str) -> None:
    record = (
        await session.execute(
            select(RefreshToken).where(RefreshToken.token_hash == hash_token(refresh_token))
        )
    ).scalar_one_or_none()
    if record is not None and record.revoked_at is None:
        record.revoked_at = datetime.now(UTC)


async def revoke_all_for_user(session: AsyncSession, user_id: uuid.UUID) -> int:
    records = (
        (
            await session.execute(
                select(RefreshToken).where(
                    RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None)
                )
            )
        )
        .scalars()
        .all()
    )
    now = datetime.now(UTC)
    for record in records:
        record.revoked_at = now
    return len(records)


# --------------------------------------------------------------------------- #
# Team management
# --------------------------------------------------------------------------- #
async def create_user(session: AsyncSession, business_id: uuid.UUID, payload: UserCreate) -> User:
    existing = await session.execute(
        tenant_select(User, business_id).where(func.lower(User.email) == payload.email.lower())
    )
    if existing.scalar_one_or_none() is not None:
        raise ConflictError("A user with this email already exists in this business.")

    user = User(
        business_id=business_id,
        email=payload.email.lower(),
        full_name=payload.full_name,
        phone=payload.phone,
        password_hash=hash_password(payload.password),
        role=payload.role,
        is_active=True,
    )
    session.add(user)
    await session.flush()
    return user


async def update_user(
    session: AsyncSession, business_id: uuid.UUID, user_id: uuid.UUID, payload: UserUpdate
) -> User:
    user = (
        await session.execute(tenant_select(User, business_id).where(User.id == user_id))
    ).scalar_one_or_none()
    if user is None:
        raise NotFoundError("User not found.")

    if payload.role is not None and user.role == UserRole.OWNER and payload.role != UserRole.OWNER:
        await _guard_last_owner(session, business_id, user.id)
    if payload.is_active is False and user.role == UserRole.OWNER:
        await _guard_last_owner(session, business_id, user.id)

    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(user, field, value)
    await session.flush()
    return user


async def soft_delete_user(
    session: AsyncSession, business_id: uuid.UUID, user_id: uuid.UUID
) -> None:
    user = (
        await session.execute(tenant_select(User, business_id).where(User.id == user_id))
    ).scalar_one_or_none()
    if user is None:
        raise NotFoundError("User not found.")
    if user.role == UserRole.OWNER:
        await _guard_last_owner(session, business_id, user.id)
    user.deleted_at = datetime.now(UTC)
    user.is_active = False
    await revoke_all_for_user(session, user.id)


async def change_password(
    session: AsyncSession, user: User, current_password: str, new_password: str
) -> None:
    if not verify_password(current_password, user.password_hash):
        raise AuthenticationError("Current password is incorrect.")
    if verify_password(new_password, user.password_hash):
        raise ValidationError("The new password must differ from the current one.")
    user.password_hash = hash_password(new_password)
    # Every existing session is invalidated on a password change.
    await revoke_all_for_user(session, user.id)


async def _guard_last_owner(
    session: AsyncSession, business_id: uuid.UUID, excluding_user_id: uuid.UUID
) -> None:
    remaining = await session.execute(
        tenant_select(User, business_id).where(
            User.role == UserRole.OWNER,
            User.is_active.is_(True),
            User.id != excluding_user_id,
        )
    )
    if remaining.scalars().first() is None:
        raise ValidationError("A business must always have at least one active owner.")
