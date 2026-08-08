"""Tenant root: businesses and their dashboard users."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, SoftDeleteMixin, TimestampMixin, UUIDPrimaryKeyMixin
from app.db.types import GUID, JSONBType
from app.models.enums import BusinessStatus, PlanTier, UserRole

if TYPE_CHECKING:
    from app.models.voice_agent import VoiceAgent


class Business(Base, UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin):
    """A tenant. Every other table carries this row's id as ``business_id``."""

    __tablename__ = "businesses"

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    slug: Mapped[str] = mapped_column(String(120), nullable=False, unique=True, index=True)
    phone: Mapped[str] = mapped_column(String(20), nullable=False)
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    industry: Mapped[str | None] = mapped_column(String(80), nullable=True)
    gstin: Mapped[str | None] = mapped_column(String(15), nullable=True)
    address: Mapped[str | None] = mapped_column(String(500), nullable=True)
    city: Mapped[str | None] = mapped_column(String(120), nullable=True)
    state: Mapped[str | None] = mapped_column(String(120), nullable=True)

    plan: Mapped[str] = mapped_column(String(30), default=PlanTier.STARTER, nullable=False)
    status: Mapped[str] = mapped_column(String(30), default=BusinessStatus.TRIAL, nullable=False)
    trial_ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    #: Free-form tenant configuration (business hours, escalation contacts, ...).
    settings_json: Mapped[dict] = mapped_column(JSONBType, default=dict, nullable=False)

    users: Mapped[list[User]] = relationship(back_populates="business", lazy="selectin")
    agents: Mapped[list[VoiceAgent]] = relationship(back_populates="business", lazy="noload")

    __table_args__ = (Index("ix_businesses_status_deleted", "status", "deleted_at"),)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Business {self.slug}>"


class User(Base, UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin):
    """A dashboard user. Scoped to exactly one business."""

    __tablename__ = "users"

    business_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False, index=True
    )
    email: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    full_name: Mapped[str] = mapped_column(String(200), nullable=False)
    phone: Mapped[str | None] = mapped_column(String(20), nullable=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(30), default=UserRole.OWNER, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    business: Mapped[Business] = relationship(back_populates="users", lazy="joined")

    __table_args__ = (
        UniqueConstraint("business_id", "email", name="uq_users_business_id_email"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<User {self.id} role={self.role}>"


class RefreshToken(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Server-side record of issued refresh tokens, so they can be revoked."""

    __tablename__ = "refresh_tokens"

    business_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: SHA-256 of the token value — the raw token is never stored.
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(300), nullable=True)
