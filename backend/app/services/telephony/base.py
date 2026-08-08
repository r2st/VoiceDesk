"""Telephony provider interface.

Concrete providers (Exotel, Knowlarity) and the in-process mock used by tests
and CI all implement :class:`TelephonyProvider`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from app.models.enums import CallStatus


@dataclass(slots=True)
class CallRequest:
    to_number: str
    from_number: str
    callback_url: str
    #: Correlates the provider call back to our ``call_logs`` row.
    call_id: str
    record: bool = True
    timeout_seconds: int = 45
    caller_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class CallResult:
    provider_call_id: str
    status: CallStatus
    raw: dict[str, Any] = field(default_factory=dict)
    error_message: str | None = None


@dataclass(slots=True)
class ProvisionedNumber:
    number: str
    provider_number_id: str
    region: str | None = None
    monthly_rent_paise: int = 0
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class WebhookEvent:
    """A normalised telephony event, whatever the provider's wire format."""

    provider_call_id: str
    status: CallStatus
    #: Our own call id when the provider echoed it back.
    call_id: str | None = None
    duration_sec: int = 0
    recording_url: str | None = None
    from_number: str | None = None
    to_number: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


class TelephonyProvider(ABC):
    """Provider-agnostic telephony operations."""

    name: str = "base"

    @abstractmethod
    async def initiate_call(self, request: CallRequest) -> CallResult:
        """Place an outbound call."""

    @abstractmethod
    async def hangup(self, provider_call_id: str) -> bool:
        """End an in-progress call."""

    @abstractmethod
    async def provision_number(
        self, *, region: str | None = None, number: str | None = None
    ) -> ProvisionedNumber:
        """Allocate a phone number from the provider's inventory."""

    @abstractmethod
    async def release_number(self, provider_number_id: str) -> bool:
        """Return a number to the provider."""

    @abstractmethod
    def parse_webhook(self, payload: dict[str, Any]) -> WebhookEvent:
        """Normalise a provider webhook body into a :class:`WebhookEvent`."""

    async def fetch_recording(self, url: str) -> bytes:  # pragma: no cover - network
        raise NotImplementedError
