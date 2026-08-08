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

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        client = self._http()
        owns_client = self._client is None
        try:
            response = await client.request(method, f"{self.base_url}{path}", **kwargs)
            if response.status_code >= 400:
                raise ExternalServiceError(
                    f"Exotel returned {response.status_code}.",
                    details={"body": response.text[:500]},
                )
            return response.json()
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
                for item in candidates
                if (number is None or item.get("PhoneNumber") == number)
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
        duration = payload.get("ConversationDuration") or payload.get("DialCallDuration") or 0
        return WebhookEvent(
            provider_call_id=str(payload.get("CallSid") or payload.get("Sid") or ""),
            status=STATUS_MAP.get(status, CallStatus.FAILED),
            call_id=payload.get("CustomField") or None,
            duration_sec=int(duration or 0),
            recording_url=payload.get("RecordingUrl") or None,
            from_number=payload.get("From") or payload.get("CallFrom"),
            to_number=payload.get("To") or payload.get("CallTo"),
            error_code=payload.get("ErrorCode"),
            error_message=payload.get("ErrorMessage"),
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
