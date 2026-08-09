"""CRM handoff for qualified leads (design doc §4.6).

Delivery is a signed webhook POST to the tenant's own endpoint (tests/test_leads.py
exercises the qualification flow itself via `FakeCrmClient`); this file covers the
real `WebhookCrmClient` on the wire, `load_crm_config`'s defensive parsing of
settings that were hand-edited or saved months ago, and the payload shape a
receiving CRM actually gets.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

import httpx

from app.core.security import sign_webhook, verify_webhook_signature
from app.models.lead import Lead
from app.services.crm import (
    CrmConfig,
    NullCrmClient,
    WebhookCrmClient,
    get_crm_client,
    lead_payload,
    load_crm_config,
    set_crm_client,
)


def make_lead(**overrides) -> Lead:
    defaults = dict(
        id=uuid.uuid4(),
        business_id=uuid.uuid4(),
        call_id=uuid.uuid4(),
        contact_name="Asha Rao",
        contact_phone="+919876543210",
        contact_email="asha@example.com",
        company="Rao Textiles",
        interest="bulk order",
        language="hi",
        source="voice_call",
        status="qualified",
        tier="hot",
        score=82,
        budget_answer="fifty thousand a month",
        budget_score=20.0,
        authority_answer="I own the business",
        authority_score=25.0,
        need_answer="need it this quarter",
        need_score=20.0,
        timeline_answer="within two weeks",
        timeline_score=17.0,
        rationale={"budget": "clear number given", "authority": "decision maker"},
        qualified_at=datetime(2026, 1, 5, 10, 30, tzinfo=UTC),
        created_at=datetime(2026, 1, 5, 10, 0, tzinfo=UTC),
    )
    defaults.update(overrides)
    return Lead(**defaults)


def make_client(*responses: httpx.Response | Exception) -> tuple[httpx.AsyncClient, list]:
    from collections import deque

    seen: list[httpx.Request] = []
    queued = deque(responses)

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        item = queued.popleft() if queued else httpx.Response(200, json={})
        if isinstance(item, Exception):
            raise item
        return item

    return httpx.AsyncClient(transport=httpx.MockTransport(handle)), seen


# --------------------------------------------------------------------------- #
# load_crm_config
# --------------------------------------------------------------------------- #
class TestLoadCrmConfig:
    def test_no_settings_at_all_is_unconfigured(self):
        assert load_crm_config(None).configured is False
        assert load_crm_config({}).configured is False

    def test_a_valid_webhook_url_is_configured(self):
        config = load_crm_config({"crm": {"webhook_url": "https://crm.example.com/hook"}})
        assert config.configured is True
        assert config.webhook_url == "https://crm.example.com/hook"

    def test_a_url_without_a_scheme_is_rejected(self):
        config = load_crm_config({"crm": {"webhook_url": "crm.example.com/hook"}})
        assert config.configured is False
        assert config.webhook_url is None

    def test_a_non_string_url_is_rejected(self):
        config = load_crm_config({"crm": {"webhook_url": 12345}})
        assert config.webhook_url is None

    def test_crm_settings_that_are_not_a_dict_are_ignored(self):
        config = load_crm_config({"crm": "not-a-dict"})
        assert config.configured is False
        assert config.headers == {}

    def test_an_empty_secret_is_treated_as_no_secret(self):
        config = load_crm_config({"crm": {"webhook_url": "https://x.test", "secret": ""}})
        assert config.secret is None

    def test_a_non_string_secret_is_ignored(self):
        config = load_crm_config({"crm": {"webhook_url": "https://x.test", "secret": 42}})
        assert config.secret is None

    def test_headers_are_coerced_to_strings(self):
        config = load_crm_config(
            {"crm": {"webhook_url": "https://x.test", "headers": {"X-Tenant": 1, 2: "ignored"}}}
        )
        assert config.headers == {"X-Tenant": "1"}

    def test_headers_that_are_not_a_dict_are_ignored(self):
        config = load_crm_config({"crm": {"webhook_url": "https://x.test", "headers": "nope"}})
        assert config.headers == {}

    def test_min_score_is_coerced_to_int(self):
        config = load_crm_config({"crm": {"webhook_url": "https://x.test", "min_score": 60.7}})
        assert config.min_score == 60

    def test_a_boolean_min_score_is_rejected(self):
        """`bool` is an `int` subclass in Python; a stray `true` must not become 1."""
        config = load_crm_config({"crm": {"webhook_url": "https://x.test", "min_score": True}})
        assert config.min_score is None

    def test_a_non_numeric_min_score_is_ignored(self):
        config = load_crm_config({"crm": {"webhook_url": "https://x.test", "min_score": "sixty"}})
        assert config.min_score is None


# --------------------------------------------------------------------------- #
# lead_payload
# --------------------------------------------------------------------------- #
class TestLeadPayload:
    def test_the_top_level_shape(self):
        lead = make_lead()
        payload = lead_payload(lead)

        assert payload["event"] == "lead.qualified"
        assert payload["lead_id"] == str(lead.id)
        assert payload["business_id"] == str(lead.business_id)
        assert payload["call_id"] == str(lead.call_id)
        assert payload["tier"] == "hot"
        assert payload["score"] == 82

    def test_a_lead_with_no_call_reports_a_null_call_id(self):
        payload = lead_payload(make_lead(call_id=None))
        assert payload["call_id"] is None

    def test_contact_details_are_nested(self):
        payload = lead_payload(make_lead())
        assert payload["contact"] == {
            "name": "Asha Rao",
            "phone": "+919876543210",
            "email": "asha@example.com",
            "company": "Rao Textiles",
        }

    def test_every_bant_dimension_carries_its_answer_score_and_reason(self):
        payload = lead_payload(make_lead())

        assert payload["bant"]["budget"] == {
            "answer": "fifty thousand a month",
            "score": 20.0,
            "reason": "clear number given",
        }
        assert payload["bant"]["timeline"]["answer"] == "within two weeks"

    def test_a_missing_rationale_entry_reports_none(self):
        payload = lead_payload(make_lead(rationale={"budget": "ok"}))
        assert payload["bant"]["timeline"]["reason"] is None

    def test_timestamps_are_isoformatted(self):
        payload = lead_payload(make_lead())
        assert payload["qualified_at"] == "2026-01-05T10:30:00+00:00"
        assert payload["created_at"] == "2026-01-05T10:00:00+00:00"

    def test_an_unqualified_lead_reports_a_null_qualified_at(self):
        payload = lead_payload(make_lead(qualified_at=None))
        assert payload["qualified_at"] is None


# --------------------------------------------------------------------------- #
# WebhookCrmClient
# --------------------------------------------------------------------------- #
class TestWebhookCrmClient:
    async def test_a_successful_push_is_delivered(self):
        http, seen = make_client(httpx.Response(200, json={"id": "crm-lead-1"}))
        client = WebhookCrmClient(client=http)
        config = CrmConfig(webhook_url="https://crm.example.com/hook", secret="s3cr3t")

        result = await client.push(config, make_lead())

        assert result.delivered is True
        assert result.reference == "crm-lead-1"
        assert len(seen) == 1

    async def test_the_payload_is_hmac_signed(self):
        http, seen = make_client(httpx.Response(200, json={}))
        client = WebhookCrmClient(client=http)
        config = CrmConfig(webhook_url="https://crm.example.com/hook", secret="s3cr3t")

        await client.push(config, make_lead())

        signature = seen[0].headers["X-VoiceDesk-Signature"]
        assert signature.startswith("sha256=")
        assert verify_webhook_signature(seen[0].content, signature, "s3cr3t")

    async def test_the_body_is_the_lead_payload(self):
        http, seen = make_client(httpx.Response(200, json={}))
        client = WebhookCrmClient(client=http)
        lead = make_lead()
        config = CrmConfig(webhook_url="https://crm.example.com/hook")

        await client.push(config, lead)

        assert json.loads(seen[0].content)["lead_id"] == str(lead.id)

    async def test_custom_headers_are_included(self):
        http, seen = make_client(httpx.Response(200, json={}))
        client = WebhookCrmClient(client=http)
        config = CrmConfig(
            webhook_url="https://crm.example.com/hook", headers={"X-Tenant": "sunrise"}
        )

        await client.push(config, make_lead())

        assert seen[0].headers["X-Tenant"] == "sunrise"

    async def test_an_http_error_status_is_not_delivered(self):
        http, _ = make_client(httpx.Response(500, text="boom"))
        client = WebhookCrmClient(client=http)
        config = CrmConfig(webhook_url="https://crm.example.com/hook")

        result = await client.push(config, make_lead())

        assert result.delivered is False
        assert result.error is not None and "500" in result.error

    async def test_a_transport_error_is_not_delivered(self):
        http, _ = make_client(httpx.ConnectError("refused"))
        client = WebhookCrmClient(client=http)
        config = CrmConfig(webhook_url="https://crm.example.com/hook")

        result = await client.push(config, make_lead())

        assert result.delivered is False
        assert result.error is not None and "ConnectError" in result.error

    async def test_a_non_json_success_body_has_no_reference(self):
        http, _ = make_client(httpx.Response(200, text="OK"))
        client = WebhookCrmClient(client=http)
        config = CrmConfig(webhook_url="https://crm.example.com/hook")

        result = await client.push(config, make_lead())

        assert result.delivered is True
        assert result.reference is None

    async def test_reference_falls_back_across_known_keys(self):
        http, _ = make_client(httpx.Response(200, json={"record_id": 987}))
        client = WebhookCrmClient(client=http)
        config = CrmConfig(webhook_url="https://crm.example.com/hook")

        result = await client.push(config, make_lead())

        assert result.reference == "987"

    async def test_a_json_array_body_has_no_reference(self):
        http, _ = make_client(httpx.Response(200, json=[1, 2, 3]))
        client = WebhookCrmClient(client=http)
        config = CrmConfig(webhook_url="https://crm.example.com/hook")

        result = await client.push(config, make_lead())

        assert result.reference is None

    async def test_an_unconfigured_url_is_not_delivered_without_a_request(self):
        http, seen = make_client()
        client = WebhookCrmClient(client=http)
        config = CrmConfig(webhook_url=None)

        result = await client.push(config, make_lead())

        assert result.delivered is False
        assert seen == []


# --------------------------------------------------------------------------- #
# NullCrmClient and the registry
# --------------------------------------------------------------------------- #
class TestNullCrmClient:
    async def test_every_push_is_dropped(self):
        result = await NullCrmClient().push(CrmConfig(webhook_url="https://x.test"), make_lead())
        assert result.delivered is False
        assert result.error is not None and "disabled" in result.error


class TestCrmClientRegistry:
    def test_an_injected_client_is_returned(self):
        stub = NullCrmClient()
        set_crm_client(stub)
        try:
            assert get_crm_client() is stub
        finally:
            set_crm_client(None)

    def test_the_default_client_depends_on_the_push_setting(self, monkeypatch):
        from app.core.config import settings

        set_crm_client(None)
        monkeypatch.setattr(settings, "crm_push_enabled", False)
        try:
            assert isinstance(get_crm_client(), NullCrmClient)
        finally:
            set_crm_client(None)

    def test_pushing_is_enabled_the_client_is_a_webhook_client(self, monkeypatch):
        from app.core.config import settings

        set_crm_client(None)
        monkeypatch.setattr(settings, "crm_push_enabled", True)
        try:
            assert isinstance(get_crm_client(), WebhookCrmClient)
        finally:
            set_crm_client(None)


class TestSignWebhook:
    def test_signing_is_deterministic_for_the_same_secret(self):
        body = b'{"a": 1}'
        assert sign_webhook(body, "secret") == sign_webhook(body, "secret")

    def test_different_secrets_produce_different_signatures(self):
        body = b'{"a": 1}'
        assert sign_webhook(body, "one") != sign_webhook(body, "two")
