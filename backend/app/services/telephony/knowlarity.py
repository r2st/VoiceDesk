"""Knowlarity (SuperReceptionist) telephony provider.

https://developer.knowlarity.com/ — JSON APIs authenticated with an
``x-api-key`` header; callbacks are JSON.
"""

from __future__ import annotations

from typing import Any

import httpx

from app.core.config import settings
from app.core.errors import ExternalServiceError
from app.models.enums import CallStatus
from app.services.telephony.base import (
    CallRequest,
    CallResult,
    ProvisionedNumber,
    TelephonyProvider,
    WebhookEvent,
)

BASE_URL = "https://kpi.knowlarity.com/Basic/v1/account"

STATUS_MAP = {
    "queued": CallStatus.QUEUED,
    "initiated": CallStatus.QUEUED,
    "ringing": CallStatus.RINGING,
    "answered": CallStatus.IN_PROGRESS,
    "connected": CallStatus.IN_PROGRESS,
    "completed": CallStatus.COMPLETED,
    "missed": CallStatus.NO_ANSWER,
    "noanswer": CallStatus.NO_ANSWER,
    "busy": CallStatus.BUSY,
    "failed": CallStatus.FAILED,
    "cancelled": CallStatus.CANCELLED,
}


class KnowlarityProvider(TelephonyProvider):
    name = "knowlarity"

    def __init__(
        self,
        api_key: str | None = None,
        sr_number: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.api_key = api_key or settings.knowlarity_api_key
        self.sr_number = sr_number or settings.knowlarity_sr_number
        self._client = client

    def _http(self) -> httpx.AsyncClient:
        if self._client is not None:
            return self._client
        return httpx.AsyncClient(
            headers={"x-api-key": self.api_key, "content-type": "application/json"},
            timeout=20.0,
        )

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        client = self._http()
        owns_client = self._client is None
        try:
            response = await client.request(method, f"{BASE_URL}{path}", **kwargs)
            if response.status_code >= 400:
                raise ExternalServiceError(
                    f"Knowlarity returned {response.status_code}.",
                    details={"body": response.text[:500]},
                )
            return response.json()
        except httpx.HTTPError as exc:
            raise ExternalServiceError(f"Knowlarity request failed: {exc}") from exc
        finally:
            if owns_client:
                await client.aclose()

    async def initiate_call(self, request: CallRequest) -> CallResult:
        payload = {
            "k_number": request.from_number or self.sr_number,
            "agent_number": request.from_number,
            "customer_number": request.to_number,
            "caller_id": request.caller_id or request.from_number,
            "additional_params": {"call_id": request.call_id},
            "callback_url": request.callback_url,
            "is_record": request.record,
        }
        body = await self._request("POST", "/call/makecall", json=payload)
        success = body.get("success", {})
        return CallResult(
            provider_call_id=str(success.get("call_id") or body.get("call_id") or ""),
            status=STATUS_MAP.get(str(success.get("status", "queued")).lower(), CallStatus.QUEUED),
            raw=body,
        )

    async def hangup(self, provider_call_id: str) -> bool:
        await self._request("POST", "/call/hangup", json={"call_id": provider_call_id})
        return True

    async def provision_number(
        self, *, region: str | None = None, number: str | None = None
    ) -> ProvisionedNumber:
        body = await self._request("GET", "/numbers")
        numbers = body.get("objects", body.get("numbers", []))
        chosen = next(
            (
                item
                for item in numbers
                if (number is None or item.get("number") == number)
                and (region is None or item.get("circle") == region)
            ),
            None,
        )
        if chosen is None:
            raise ExternalServiceError(
                "No Knowlarity number is available matching the request.",
                details={"region": region, "number": number},
            )
        return ProvisionedNumber(
            number=chosen["number"],
            provider_number_id=str(chosen.get("id", chosen["number"])),
            region=chosen.get("circle"),
            raw=chosen,
        )

    async def release_number(self, provider_number_id: str) -> bool:
        await self._request("DELETE", f"/numbers/{provider_number_id}")
        return True

    def parse_webhook(self, payload: dict[str, Any]) -> WebhookEvent:
        status = str(payload.get("call_status") or payload.get("status") or "").lower()
        extra = payload.get("additional_params") or {}
        return WebhookEvent(
            provider_call_id=str(payload.get("call_id") or payload.get("uuid") or ""),
            status=STATUS_MAP.get(status, CallStatus.FAILED),
            call_id=extra.get("call_id"),
            duration_sec=int(payload.get("call_duration") or payload.get("duration") or 0),
            recording_url=payload.get("resource_url") or payload.get("recording_url"),
            from_number=payload.get("caller_id") or payload.get("customer_number"),
            to_number=payload.get("knowlarity_number") or payload.get("agent_number"),
            error_code=payload.get("error_code"),
            error_message=payload.get("error_message"),
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
