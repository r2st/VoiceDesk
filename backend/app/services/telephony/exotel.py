"""Exotel telephony provider.

https://developer.exotel.com/api/ — Exotel posts ``application/x-www-form-urlencoded``
callbacks with ``CallSid``/``Status``/``ConversationDuration`` fields.
"""

from __future__ import annotations

from typing import Any

import httpx

from app.core.config import settings
from app.core.errors import ExternalServiceError
from app.core.logging import get_logger
from app.models.enums import CallStatus
from app.services.telephony.base import (
    CallRequest,
    CallResult,
    ProvisionedNumber,
    TelephonyProvider,
    WebhookEvent,
    coerce_duration,
    coerce_text,
)

logger = get_logger(__name__)

STATUS_MAP = {
    "queued": CallStatus.QUEUED,
    "ringing": CallStatus.RINGING,
    "in-progress": CallStatus.IN_PROGRESS,
    "completed": CallStatus.COMPLETED,
    "no-answer": CallStatus.NO_ANSWER,
    "busy": CallStatus.BUSY,
    "failed": CallStatus.FAILED,
    "canceled": CallStatus.CANCELLED,
}


class ExotelProvider(TelephonyProvider):
    name = "exotel"

    def __init__(
        self,
        sid: str | None = None,
        api_key: str | None = None,
        api_token: str | None = None,
        subdomain: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.sid = sid or settings.exotel_sid
        self.api_key = api_key or settings.exotel_api_key
        self.api_token = api_token or settings.exotel_api_token
        self.subdomain = subdomain or settings.exotel_subdomain
        self._client = client

    @property
    def base_url(self) -> str:
        return f"https://{self.subdomain}/v1/Accounts/{self.sid}"

    def _http(self) -> httpx.AsyncClient:
        if self._client is not None:
            return self._client
        return httpx.AsyncClient(auth=(self.api_key, self.api_token), timeout=20.0)

    def _require_credentials(self) -> None:
        """Fail loudly on a half-configured deployment.

        Without this the adapter happily builds ``https:///v1/Accounts/`` out of
        empty settings and the first symptom is a DNS error on the call path,
        which reads like a network problem rather than a missing secret.
        """
        missing = [
            name
            for name, value in (
                ("EXOTEL_SID", self.sid),
                ("EXOTEL_API_KEY", self.api_key),
                ("EXOTEL_API_TOKEN", self.api_token),
            )
            if not value
        ]
        if missing:
            raise ExternalServiceError(f"Exotel is not configured: {', '.join(missing)} not set.")

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        self._require_credentials()
        client = self._http()
        owns_client = self._client is None
        try:
            response = await client.request(method, f"{self.base_url}{path}", **kwargs)
            if response.status_code >= 400:
                raise ExternalServiceError(
                    f"Exotel returned {response.status_code}.",
                    details={"body": response.text[:500]},
                )
            try:
                return response.json()
            except ValueError as exc:
                # A proxy or WAF in front of Exotel answering 200 with an HTML
                # page. `JSONDecodeError` is a `ValueError`, not an
                # `httpx.HTTPError`, so it escaped uncaught and reached callers
                # that only defend against `ExternalServiceError`.
                raise ExternalServiceError(
                    "Exotel returned a body that is not JSON.",
                    details={"body": response.text[:500]},
                ) from exc
        except httpx.HTTPError as exc:
            raise ExternalServiceError(f"Exotel request failed: {exc}") from exc
        finally:
            if owns_client:
                await client.aclose()

    async def initiate_call(self, request: CallRequest) -> CallResult:
        data = {
            "From": request.to_number,  # Exotel dials the customer leg first
            "To": request.from_number,
            "CallerId": request.caller_id or request.from_number,
            "CallType": "trans",
            "TimeLimit": str(request.timeout_seconds * 60),
            "TimeOut": str(request.timeout_seconds),
            "StatusCallback": request.callback_url,
            "StatusCallbackEvents[0]": "terminal",
            "Record": "true" if request.record else "false",
            "CustomField": request.call_id,
        }
        body = await self._request("POST", "/Calls/connect.json", data=data)
        call = body.get("Call", {})
        return CallResult(
            provider_call_id=str(call.get("Sid", "")),
            status=STATUS_MAP.get(str(call.get("Status", "queued")).lower(), CallStatus.QUEUED),
            raw=body,
        )

    async def hangup(self, provider_call_id: str) -> bool:
        await self._request("POST", f"/Calls/{provider_call_id}.json", data={"Status": "completed"})
        return True

    async def provision_number(
        self, *, region: str | None = None, number: str | None = None
    ) -> ProvisionedNumber:
        # Exotel numbers are allocated through their dashboard/account team; the
        # API only exposes the numbers already on the account.
        body = await self._request("GET", "/IncomingPhoneNumbers.json")
        candidates = body.get("IncomingPhoneNumbers", [])
        chosen = next(
            (
                item
                # A row without a number is unusable, and indexing it below
                # raised `KeyError` — an unhandled 500 rather than the 502 that
                # callers translate `ExternalServiceError` into. Skipping it
                # also lets a later, complete row satisfy the request.
                for item in candidates
                if item.get("PhoneNumber")
                and (number is None or item.get("PhoneNumber") == number)
                and (region is None or item.get("Region") == region)
            ),
            None,
        )
        if chosen is None:
            raise ExternalServiceError(
                "No Exotel number is available matching the request.",
                details={"region": region, "number": number},
            )
        return ProvisionedNumber(
            number=chosen["PhoneNumber"],
            provider_number_id=str(chosen.get("Sid", chosen["PhoneNumber"])),
            region=chosen.get("Region"),
            raw=chosen,
        )

    async def release_number(self, provider_number_id: str) -> bool:
        logger.info("Exotel numbers are released via the account dashboard; marking released.")
        return True

    def parse_webhook(self, payload: dict[str, Any]) -> WebhookEvent:
        status = str(payload.get("Status") or payload.get("CallStatus") or "").lower()
        return WebhookEvent(
            provider_call_id=str(payload.get("CallSid") or payload.get("Sid") or ""),
            # Deliberately no fallback: an unknown or absent status leaves this
            # `None` so the call keeps the state it already had.
            status=STATUS_MAP.get(status),
            call_id=payload.get("CustomField") or None,
            duration_sec=coerce_duration(
                payload.get("ConversationDuration"), payload.get("DialCallDuration")
            ),
            recording_url=payload.get("RecordingUrl") or None,
            from_number=payload.get("From") or payload.get("CallFrom"),
            to_number=payload.get("To") or payload.get("CallTo"),
            error_code=coerce_text(payload.get("ErrorCode")),
            error_message=coerce_text(payload.get("ErrorMessage")),
            raw=payload,
        )

    async def fetch_recording(self, url: str) -> bytes:
        client = self._http()
        owns_client = self._client is None
        try:
            response = await client.get(url)
            if response.status_code >= 400:
                raise ExternalServiceError(f"Recording download failed ({response.status_code}).")
            return response.content
        except httpx.HTTPError as exc:
            raise ExternalServiceError(f"Recording download failed: {exc}") from exc
        finally:
            if owns_client:
                await client.aclose()
