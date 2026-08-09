"""CRM handoff for qualified leads (design doc §4.6).

A qualified lead is only worth as much as the speed it reaches a salesperson,
and most of our tenants already live in a CRM rather than in this dashboard. So
each tenant can register a webhook that receives its leads as they qualify.

Delivery is a push to the tenant's own endpoint rather than an integration per
CRM vendor: the shapes differ, but every CRM worth naming can receive a signed
JSON POST, and a tenant with something unusual can point the webhook at their
own glue. Payloads are HMAC-signed the same way inbound webhooks are verified
(§8.3), so a receiver can tell a real lead from anything else that finds the
URL.

Configuration lives on ``Business.settings_json``::

    {
      "crm": {
        "webhook_url": "https://crm.example.com/hooks/voicedesk",
        "secret": "...",
        "headers": {"X-Tenant": "sunrise"},
        "min_score": 60
      }
    }
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

import httpx

from app.core.config import settings
from app.core.logging import get_logger
from app.core.security import sign_webhook
from app.models.lead import Lead

logger = get_logger(__name__)

#: A CRM that has not answered in this long is treated as down for this pass.
REQUEST_TIMEOUT_SECONDS = 10.0


@dataclass(frozen=True, slots=True)
class CrmConfig:
    """A tenant's resolved CRM destination."""

    webhook_url: str | None = None
    secret: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    #: Leads below this never leave the platform. Defaults to "whatever
    #: qualified", which is the tenant's own ``qualify_at``.
    min_score: int | None = None

    @property
    def configured(self) -> bool:
        return bool(self.webhook_url)


def load_crm_config(settings_json: dict | None) -> CrmConfig:
    """Resolve a tenant's CRM settings, ignoring anything malformed.

    A bad value saved months ago must not take the push job down for every
    other tenant in the same pass, so this never raises.
    """
    raw = (settings_json or {}).get("crm")
    options: dict = raw if isinstance(raw, dict) else {}

    url = options.get("webhook_url")
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        url = None

    secret = options.get("secret")
    headers = options.get("headers")
    min_score = options.get("min_score")

    return CrmConfig(
        webhook_url=url,
        secret=secret if isinstance(secret, str) and secret else None,
        headers={
            str(k): str(v)
            for k, v in (headers.items() if isinstance(headers, dict) else ())
            if isinstance(k, str)
        },
        min_score=(
            int(min_score)
            if isinstance(min_score, (int, float)) and not isinstance(min_score, bool)
            else None
        ),
    )


def lead_payload(lead: Lead) -> dict:
    """The lead as a CRM sees it.

    Both the score and the answers behind it are sent: a sales manager reading
    this in their own system should be able to question the number without
    coming back here for the transcript.
    """
    return {
        "event": "lead.qualified",
        "lead_id": str(lead.id),
        "business_id": str(lead.business_id),
        "call_id": str(lead.call_id) if lead.call_id else None,
        "contact": {
            "name": lead.contact_name,
            "phone": lead.contact_phone,
            "email": lead.contact_email,
            "company": lead.company,
        },
        "interest": lead.interest,
        "language": lead.language,
        "source": lead.source,
        "status": lead.status,
        "tier": lead.tier,
        "score": lead.score,
        "bant": {
            dimension: {
                "answer": getattr(lead, f"{dimension}_answer"),
                "score": getattr(lead, f"{dimension}_score"),
                "reason": (lead.rationale or {}).get(dimension),
            }
            for dimension in ("budget", "authority", "need", "timeline")
        },
        "qualified_at": lead.qualified_at.isoformat() if lead.qualified_at else None,
        "created_at": lead.created_at.isoformat() if lead.created_at else None,
    }


@dataclass(slots=True)
class CrmResult:
    """The outcome of one delivery attempt."""

    delivered: bool
    reference: str | None = None
    error: str | None = None


class CrmClient(ABC):
    """Delivers a lead to a tenant's CRM."""

    name = "base"

    @abstractmethod
    async def push(self, config: CrmConfig, lead: Lead) -> CrmResult: ...


class WebhookCrmClient(CrmClient):
    """POSTs the lead to the tenant's endpoint, HMAC-signed."""

    name = "webhook"

    async def push(self, config: CrmConfig, lead: Lead) -> CrmResult:
        if not config.webhook_url:  # pragma: no cover - callers check first
            return CrmResult(delivered=False, error="No CRM webhook configured.")

        body = json.dumps(lead_payload(lead), separators=(",", ":")).encode()
        headers = {
            "Content-Type": "application/json",
            "X-VoiceDesk-Event": "lead.qualified",
            "X-VoiceDesk-Signature": f"sha256={sign_webhook(body, config.secret)}",
            **config.headers,
        }

        try:
            async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
                response = await client.post(config.webhook_url, content=body, headers=headers)
        except httpx.HTTPError as exc:
            return CrmResult(delivered=False, error=f"{type(exc).__name__}: {exc}"[:500])

        if response.status_code >= 400:
            return CrmResult(
                delivered=False,
                error=f"CRM returned HTTP {response.status_code}: {response.text[:200]}",
            )
        return CrmResult(delivered=True, reference=_reference(response))


def _reference(response: httpx.Response) -> str | None:
    """The CRM's own id for the lead, when it hands one back.

    Stored so a salesperson looking at a lead here can be pointed at the record
    in the system they actually work in.
    """
    try:
        payload = response.json()
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    for key in ("id", "lead_id", "record_id", "reference"):
        value = payload.get(key)
        if isinstance(value, (str, int)):
            return str(value)[:160]
    return None


class NullCrmClient(CrmClient):
    """Drops every push. Used when outbound CRM delivery is switched off."""

    name = "null"

    async def push(self, config: CrmConfig, lead: Lead) -> CrmResult:  # noqa: ARG002
        logger.debug("CRM delivery disabled; dropping lead %s", lead.id)
        return CrmResult(delivered=False, error="CRM delivery is disabled.")


_client: CrmClient | None = None


def get_crm_client() -> CrmClient:
    global _client
    if _client is None:
        _client = WebhookCrmClient() if settings.crm_push_enabled else NullCrmClient()
    return _client


def set_crm_client(client: CrmClient | None) -> None:
    """Test seam — pass ``None`` to restore the configured client."""
    global _client
    _client = client
