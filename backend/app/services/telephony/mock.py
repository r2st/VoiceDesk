"""In-process mock telephony provider (design doc §9.2).

Simulates Exotel/Knowlarity for development, tests and CI. Every interaction is
recorded on :attr:`MockTelephonyProvider.interactions` so tests can assert on
what the platform asked the network to do. Call outcomes are scriptable.
"""

from __future__ import annotations

import itertools
from collections import deque
from typing import Any

from app.models.enums import CallStatus
from app.services.telephony.base import (
    CallRequest,
    CallResult,
    ProvisionedNumber,
    TelephonyProvider,
    WebhookEvent,
)

_STATUS_ALIASES = {
    "queued": CallStatus.QUEUED,
    "ringing": CallStatus.RINGING,
    "in-progress": CallStatus.IN_PROGRESS,
    "in_progress": CallStatus.IN_PROGRESS,
    "answered": CallStatus.IN_PROGRESS,
    "completed": CallStatus.COMPLETED,
    "complete": CallStatus.COMPLETED,
    "no-answer": CallStatus.NO_ANSWER,
    "no_answer": CallStatus.NO_ANSWER,
    "busy": CallStatus.BUSY,
    "failed": CallStatus.FAILED,
    "canceled": CallStatus.CANCELLED,
    "cancelled": CallStatus.CANCELLED,
}


class MockTelephonyProvider(TelephonyProvider):
    """A deterministic telephony stand-in.

    ``scenarios`` scripts the outcome of successive ``initiate_call`` calls, e.g.
    ``["pickup", "no-answer", "busy", "network-error"]``. When the script runs
    out, every subsequent call is a pickup.
    """

    name = "mock"

    SCENARIOS = {
        "pickup": CallStatus.RINGING,
        "no-answer": CallStatus.NO_ANSWER,
        "busy": CallStatus.BUSY,
        "network-error": CallStatus.FAILED,
    }

    def __init__(self, scenarios: list[str] | None = None) -> None:
        self.interactions: list[dict[str, Any]] = []
        self._scenarios: deque[str] = deque(scenarios or [])
        self._counter = itertools.count(1)
        self._numbers: dict[str, ProvisionedNumber] = {}
        self._live_calls: set[str] = set()

    # -- scripting ---------------------------------------------------------- #
    def queue_scenario(self, *scenarios: str) -> None:
        self._scenarios.extend(scenarios)

    def reset(self) -> None:
        self.interactions.clear()
        self._scenarios.clear()
        self._live_calls.clear()

    def _next_scenario(self) -> str:
        return self._scenarios.popleft() if self._scenarios else "pickup"

    def _record(self, action: str, **fields: Any) -> None:
        self.interactions.append({"action": action, **fields})

    # -- TelephonyProvider -------------------------------------------------- #
    async def initiate_call(self, request: CallRequest) -> CallResult:
        scenario = self._next_scenario()
        status = self.SCENARIOS.get(scenario, CallStatus.RINGING)
        provider_call_id = f"mock-call-{next(self._counter):06d}"

        self._record(
            "initiate_call",
            to=request.to_number,
            from_=request.from_number,
            call_id=request.call_id,
            record=request.record,
            scenario=scenario,
            provider_call_id=provider_call_id,
        )
        if status in (CallStatus.RINGING, CallStatus.QUEUED):
            self._live_calls.add(provider_call_id)

        return CallResult(
            provider_call_id=provider_call_id,
            status=status,
            raw={"scenario": scenario, "callback_url": request.callback_url},
            error_message="Simulated network error" if scenario == "network-error" else None,
        )

    async def hangup(self, provider_call_id: str) -> bool:
        self._record("hangup", provider_call_id=provider_call_id)
        return self._live_calls.discard(provider_call_id) is None and True

    async def provision_number(
        self, *, region: str | None = None, number: str | None = None
    ) -> ProvisionedNumber:
        allocated = number or f"+9180{next(self._counter):08d}"
        provisioned = ProvisionedNumber(
            number=allocated,
            provider_number_id=f"mock-num-{next(self._counter):04d}",
            region=region or "Bengaluru",
            # ₹499/month for a virtual number.
            monthly_rent_paise=49_900,
            raw={"provider": "mock"},
        )
        self._numbers[provisioned.provider_number_id] = provisioned
        self._record("provision_number", number=allocated, region=region)
        return provisioned

    async def release_number(self, provider_number_id: str) -> bool:
        self._record("release_number", provider_number_id=provider_number_id)
        return self._numbers.pop(provider_number_id, None) is not None

    def parse_webhook(self, payload: dict[str, Any]) -> WebhookEvent:
        # Omitting ``status`` means "the call finished" — a scripting shorthand
        # the real providers do not have. An unrecognised status still parses as
        # ``None``, matching them, so it cannot end a call by accident.
        raw_status = str(payload.get("status", "completed")).lower()
        return WebhookEvent(
            provider_call_id=str(payload.get("provider_call_id") or payload.get("CallSid") or ""),
            status=_STATUS_ALIASES.get(raw_status),
            call_id=payload.get("call_id"),
            duration_sec=int(payload.get("duration_sec") or 0),
            recording_url=payload.get("recording_url"),
            from_number=payload.get("from"),
            to_number=payload.get("to"),
            error_code=payload.get("error_code"),
            error_message=payload.get("error_message"),
            raw=payload,
        )

    async def fetch_recording(self, url: str) -> bytes:
        self._record("fetch_recording", url=url)
        # A tiny deterministic byte string standing in for Opus audio.
        return b"MOCK_OPUS_AUDIO" + url.encode()[-16:]
