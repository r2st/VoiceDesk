"""Telephony provider registry."""

from __future__ import annotations

from app.core.config import settings
from app.models.enums import TelephonyProvider as ProviderName
from app.services.telephony.base import (
    CallRequest,
    CallResult,
    ProvisionedNumber,
    TelephonyProvider,
    WebhookEvent,
)
from app.services.telephony.exotel import ExotelProvider
from app.services.telephony.knowlarity import KnowlarityProvider
from app.services.telephony.mock import MockTelephonyProvider

_override: TelephonyProvider | None = None
_cache: dict[str, TelephonyProvider] = {}


def get_provider(name: str | None = None) -> TelephonyProvider:
    """Resolve a provider by name, defaulting to ``settings.telephony_provider``.

    Tests call :func:`set_provider_override` to force the mock everywhere.
    """
    if _override is not None:
        return _override

    key = (name or settings.telephony_provider or ProviderName.MOCK).lower()
    if key not in _cache:
        if key == ProviderName.EXOTEL:
            _cache[key] = ExotelProvider()
        elif key == ProviderName.KNOWLARITY:
            _cache[key] = KnowlarityProvider()
        else:
            _cache[key] = MockTelephonyProvider()
    return _cache[key]


def set_provider_override(provider: TelephonyProvider | None) -> None:
    global _override
    _override = provider


def reset_providers() -> None:
    _cache.clear()
    set_provider_override(None)


__all__ = [
    "CallRequest",
    "CallResult",
    "ExotelProvider",
    "KnowlarityProvider",
    "MockTelephonyProvider",
    "ProvisionedNumber",
    "TelephonyProvider",
    "WebhookEvent",
    "get_provider",
    "reset_providers",
    "set_provider_override",
]
