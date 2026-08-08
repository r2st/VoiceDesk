"""Shared schema primitives."""

from __future__ import annotations

import re
from typing import Annotated, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator

T = TypeVar("T")

#: E.164, biased towards Indian numbers but not restricted to them.
PHONE_RE = re.compile(r"^\+?[1-9]\d{7,14}$")


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class Page(BaseModel, Generic[T]):
    """Offset-paginated envelope."""

    items: list[T]
    total: int
    limit: int
    offset: int

    @property
    def has_more(self) -> bool:
        return self.offset + len(self.items) < self.total


class PaginationParams(BaseModel):
    limit: Annotated[int, Field(ge=1, le=200)] = 50
    offset: Annotated[int, Field(ge=0)] = 0


def normalize_phone(value: str, default_country_code: str = "91") -> str:
    """Normalise an Indian phone number to E.164 (``+91XXXXXXXXXX``).

    Accepts ``9876543210``, ``09876543210``, ``+91 98765 43210`` and
    ``91-9876543210``; anything that cannot be resolved raises ``ValueError``.
    """
    if value is None:
        raise ValueError("Phone number is required.")
    cleaned = re.sub(r"[\s\-().]", "", str(value).strip())
    if cleaned.startswith("+"):
        digits = cleaned[1:]
    else:
        digits = cleaned.lstrip("0")
        if len(digits) == 10:
            digits = f"{default_country_code}{digits}"
    if not digits.isdigit():
        raise ValueError(f"Invalid phone number: {value!r}")
    candidate = f"+{digits}"
    if not PHONE_RE.match(candidate):
        raise ValueError(f"Invalid phone number: {value!r}")
    return candidate


class PhoneMixin(BaseModel):
    """Mixin normalising any field literally named ``phone``."""

    @field_validator("phone", mode="before", check_fields=False)
    @classmethod
    def _normalize(cls, value: object) -> object:
        if isinstance(value, str) and value:
            return normalize_phone(value)
        return value


class MessageResponse(BaseModel):
    message: str
    ok: bool = True
