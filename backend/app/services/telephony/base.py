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
    #: ``None`` when the callback said nothing about the call's state — either
    #: it carried no status at all (a recording-ready ping) or one this code
    #: does not know. Consumers must leave the call's status alone in that
    #: case: guessing used to mean ``FAILED``, which is terminal, so a stray
    #: callback ended a live call and metered it for billing.
    status: CallStatus | None = None
    #: Our own call id when the provider echoed it back.
    call_id: str | None = None
    duration_sec: int = 0
    recording_url: str | None = None
    #: True when ``recording_url`` is a caller's voicemail message rather than
    #: the recording of an answered call.
    is_voicemail: bool = False
    from_number: str | None = None
    to_number: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


def coerce_duration(*candidates: Any) -> int:
    """Seconds, from the first candidate that reads as a number.

    Providers disagree about the wire type: Exotel form-encodes everything, so
    a duration arrives as ``"45"`` or ``"45.0"``, while Knowlarity sends JSON
    numbers. A value that is not a number at all must not raise — webhooks are
    parsed inside the request handler, so an exception is a 500, and a 500 is
    answered by redelivering the same body every few minutes indefinitely.
    """
    for value in candidates:
        if value is None or value == "":
            continue
        try:
            return max(0, int(float(value)))
        except (TypeError, ValueError):
            continue
    return 0


def coerce_text(value: Any) -> str | None:
    """A provider field as text. Error codes arrive as ints as often as strings."""
    if value is None or value == "":
        return None
    return str(value)


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
