"""Auth and tenant-registration schemas."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, EmailStr, Field, field_validator

from app.models.enums import BusinessStatus, PlanTier, UserRole
from app.schemas.common import ORMModel, normalize_phone

Password = Annotated[str, Field(min_length=10, max_length=72)]


class RegisterRequest(BaseModel):
    """Creates a business plus its first owner user in one call."""

    business_name: Annotated[str, Field(min_length=2, max_length=200)]
    business_phone: str
    industry: str | None = None
    gstin: Annotated[str | None, Field(default=None, max_length=15)] = None
    city: str | None = None
    state: str | None = None

    full_name: Annotated[str, Field(min_length=2, max_length=200)]
    email: EmailStr
    password: Password
    plan: PlanTier = PlanTier.STARTER

    @field_validator("business_phone")
    @classmethod
    def _phone(cls, value: str) -> str:
        return normalize_phone(value)

    @field_validator("password")
    @classmethod
    def _password_strength(cls, value: str) -> str:
        if value.isdigit() or value.isalpha():
            raise ValueError("Password must mix letters and digits.")
        return value


class LoginRequest(BaseModel):
    email: EmailStr
    password: str
    #: Optional; disambiguates a user who exists in more than one business.
    business_slug: str | None = None


class RefreshRequest(BaseModel):
    refresh_token: str


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int
    expires_at: datetime


class UserOut(ORMModel):
    id: uuid.UUID
    business_id: uuid.UUID
    email: str
    full_name: str
    phone: str | None = None
    role: UserRole
    is_active: bool
    last_login_at: datetime | None = None
    created_at: datetime


class BusinessOut(ORMModel):
    id: uuid.UUID
    name: str
    slug: str
    phone: str
    email: str
    industry: str | None = None
    gstin: str | None = None
    city: str | None = None
    state: str | None = None
    plan: PlanTier
    status: BusinessStatus
    trial_ends_at: datetime | None = None
    settings_json: dict = {}
    created_at: datetime


class RegisterResponse(BaseModel):
    business: BusinessOut
    user: UserOut
    tokens: TokenPair


class BusinessUpdate(BaseModel):
    name: Annotated[str | None, Field(default=None, min_length=2, max_length=200)] = None
    phone: str | None = None
    industry: str | None = None
    gstin: Annotated[str | None, Field(default=None, max_length=15)] = None
    address: str | None = None
    city: str | None = None
    state: str | None = None
    settings_json: dict | None = None

    @field_validator("phone")
    @classmethod
    def _phone(cls, value: str | None) -> str | None:
        return normalize_phone(value) if value else value


class UserCreate(BaseModel):
    email: EmailStr
    full_name: Annotated[str, Field(min_length=2, max_length=200)]
    password: Password
    role: UserRole = UserRole.VIEWER
    phone: str | None = None

    @field_validator("phone")
    @classmethod
    def _phone(cls, value: str | None) -> str | None:
        return normalize_phone(value) if value else value


class UserUpdate(BaseModel):
    full_name: Annotated[str | None, Field(default=None, min_length=2, max_length=200)] = None
    role: UserRole | None = None
    is_active: bool | None = None
    phone: str | None = None


class PasswordChange(BaseModel):
    current_password: str
    new_password: Password
