"""Exotel and Knowlarity telephony adapters (design doc §4.1, §9.2).

These two classes are the whole boundary between the platform and the phone
network, and almost everything they handle is written by someone else: Exotel
posts form-encoded callbacks where every value is a string, Knowlarity posts
JSON where numbers stay numbers, and neither is obliged to keep sending the
fields this code was written against.

So the tests here lean on the hostile cases rather than the happy path. A
provider webhook that raises is not a contained failure: `parse_webhook` runs
inline in the request handler, so an exception is a 500, and a 500 makes the
provider redeliver the same body every few minutes forever.

The HTTP calls are exercised through an injected `httpx.AsyncClient` backed by
`MockTransport`, so the request the adapter actually built can be asserted on
without any network.
"""

from __future__ import annotations

import json as jsonlib
from collections import deque
from dataclasses import replace
from urllib.parse import parse_qsl

import httpx
import pytest

from app.core.errors import ExternalServiceError
from app.models.enums import CallStatus
from app.services import telephony
from app.services.telephony import (
    CallRequest,
    ExotelProvider,
    KnowlarityProvider,
    MockTelephonyProvider,
)

EXOTEL_CREDS = {
    "sid": "sunrise",
    "api_key": "key-123",
    "api_token": "token-456",
    "subdomain": "api.exotel.com",
}


def make_client(*responses: httpx.Response | Exception) -> tuple[httpx.AsyncClient, list]:
    """An `AsyncClient` that replays `responses` and records what it was sent.

    An `Exception` in the script is raised instead of returned, which is how the
    transport-level failures (DNS, timeout, reset) are simulated. Once the
    script runs out every further request gets an empty 200.
    """
    seen: list[httpx.Request] = []
    queued = deque(responses)

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        item = queued.popleft() if queued else httpx.Response(200, json={})
        if isinstance(item, Exception):
            raise item
        return item

    return httpx.AsyncClient(transport=httpx.MockTransport(handle)), seen


def form_of(request: httpx.Request) -> dict[str, str]:
    """The form-encoded body Exotel was sent, as a dict."""
    return dict(parse_qsl(request.content.decode()))


def exotel(*responses: httpx.Response | Exception) -> tuple[ExotelProvider, list]:
    client, seen = make_client(*responses)
    return ExotelProvider(**EXOTEL_CREDS, client=client), seen


def knowlarity(*responses: httpx.Response | Exception) -> tuple[KnowlarityProvider, list]:
    client, seen = make_client(*responses)
    return KnowlarityProvider(api_key="key-123", sr_number="+918040000000", client=client), seen


def call_request(**overrides) -> CallRequest:
    base = CallRequest(
        to_number="+919999988888",
        from_number="+918000000001",
        callback_url="https://voicedesk.test/webhooks/telephony",
        call_id="11111111-2222-3333-4444-555555555555",
    )
    return replace(base, **overrides) if overrides else base


# --------------------------------------------------------------------------- #
# Exotel — placing a call
# --------------------------------------------------------------------------- #
class TestExotelInitiateCall:
    async def test_dials_the_customer_leg_first(self):
        """Exotel's `From` is the leg it rings first, which is the customer.

        This reads backwards against our own `CallRequest`, where `from_number`
        is the business's virtual number. Getting the two the wrong way round
        would ring the business's own line and connect it to itself, so it is
        worth pinning explicitly rather than leaving to the reader.
        """
        provider, seen = exotel(
            httpx.Response(200, json={"Call": {"Sid": "exo-1", "Status": "queued"}})
        )

        await provider.initiate_call(call_request())

        body = form_of(seen[0])
        assert body["From"] == "+919999988888"  # the customer
        assert body["To"] == "+918000000001"  # the business's virtual number

    async def test_sends_our_call_id_so_the_callback_can_be_correlated(self):
        """`CustomField` is the only thing tying the callback to our row."""
        provider, seen = exotel(
            httpx.Response(200, json={"Call": {"Sid": "exo-1", "Status": "queued"}})
        )

        await provider.initiate_call(call_request(call_id="abc-123"))

        assert form_of(seen[0])["CustomField"] == "abc-123"

    async def test_posts_to_the_accounts_connect_endpoint(self):
        provider, seen = exotel(
            httpx.Response(200, json={"Call": {"Sid": "exo-1", "Status": "queued"}})
        )

        await provider.initiate_call(call_request())

        assert seen[0].method == "POST"
        assert str(seen[0].url) == "https://api.exotel.com/v1/Accounts/sunrise/Calls/connect.json"

    async def test_caller_id_defaults_to_the_business_number(self):
        provider, seen = exotel(
            httpx.Response(200, json={"Call": {"Sid": "exo-1", "Status": "queued"}})
        )

        await provider.initiate_call(call_request())

        assert form_of(seen[0])["CallerId"] == "+918000000001"

    async def test_an_explicit_caller_id_is_used_when_given(self):
        provider, seen = exotel(
            httpx.Response(200, json={"Call": {"Sid": "exo-1", "Status": "queued"}})
        )

        await provider.initiate_call(call_request(caller_id="+918040001111"))

        assert form_of(seen[0])["CallerId"] == "+918040001111"

    @pytest.mark.parametrize("record, expected", [(True, "true"), (False, "false")])
    async def test_recording_flag_is_passed_through(self, record: bool, expected: str):
        provider, seen = exotel(
            httpx.Response(200, json={"Call": {"Sid": "exo-1", "Status": "queued"}})
        )

        await provider.initiate_call(call_request(record=record))

        assert form_of(seen[0])["Record"] == expected

    async def test_ring_timeout_is_sent_in_seconds(self):
        provider, seen = exotel(
            httpx.Response(200, json={"Call": {"Sid": "exo-1", "Status": "queued"}})
        )

        await provider.initiate_call(call_request(timeout_seconds=20))

        assert form_of(seen[0])["TimeOut"] == "20"

    async def test_returns_the_provider_call_id_and_status(self):
        provider, _ = exotel(
            httpx.Response(200, json={"Call": {"Sid": "exo-9", "Status": "ringing"}})
        )

        result = await provider.initiate_call(call_request())

        assert result.provider_call_id == "exo-9"
        assert result.status == CallStatus.RINGING

    async def test_an_unrecognised_status_on_a_placed_call_is_queued(self):
        """The call was accepted, so the safe reading is "not started yet"."""
        provider, _ = exotel(
            httpx.Response(200, json={"Call": {"Sid": "exo-9", "Status": "wobbling"}})
        )

        result = await provider.initiate_call(call_request())

        assert result.status == CallStatus.QUEUED


# --------------------------------------------------------------------------- #
# Exotel — transport failures
# --------------------------------------------------------------------------- #
class TestExotelTransport:
    async def test_a_4xx_becomes_an_external_service_error(self):
        provider, _ = exotel(httpx.Response(403, text="forbidden"))

        with pytest.raises(ExternalServiceError) as exc:
            await provider.initiate_call(call_request())

        assert "403" in str(exc.value)

    async def test_a_5xx_becomes_an_external_service_error(self):
        provider, _ = exotel(httpx.Response(503, text="upstream down"))

        with pytest.raises(ExternalServiceError):
            await provider.initiate_call(call_request())

    async def test_the_error_body_is_captured_for_debugging(self):
        provider, _ = exotel(httpx.Response(400, text="CallerId not on account"))

        with pytest.raises(ExternalServiceError) as exc:
            await provider.initiate_call(call_request())

        assert "CallerId not on account" in exc.value.details["body"]

    async def test_a_huge_error_body_is_truncated(self):
        provider, _ = exotel(httpx.Response(400, text="x" * 5000))

        with pytest.raises(ExternalServiceError) as exc:
            await provider.initiate_call(call_request())

        assert len(exc.value.details["body"]) <= 500

    async def test_a_network_failure_becomes_an_external_service_error(self):
        provider, _ = exotel(httpx.ConnectError("name resolution failed"))

        with pytest.raises(ExternalServiceError):
            await provider.initiate_call(call_request())

    async def test_a_timeout_becomes_an_external_service_error(self):
        provider, _ = exotel(httpx.ReadTimeout("timed out"))

        with pytest.raises(ExternalServiceError):
            await provider.initiate_call(call_request())

    async def test_a_non_json_200_becomes_an_external_service_error(self):
        """A proxy or WAF between us and Exotel answers 200 with an HTML page.

        Decoding that raises `JSONDecodeError`, which is a `ValueError` and not
        an `httpx.HTTPError`, so it escaped the adapter uncaught and reached
        callers that only defend against `ExternalServiceError`.
        """
        provider, _ = exotel(httpx.Response(200, text="<html>Gateway Timeout</html>"))

        with pytest.raises(ExternalServiceError):
            await provider.initiate_call(call_request())

    async def test_missing_credentials_are_reported_as_configuration(self):
        """Left unconfigured, the adapter used to build `https:///v1/Accounts/`.

        That surfaces as a confusing DNS or connection error at call time. A
        deployment that forgot the Exotel keys should be told so.
        """
        client, seen = make_client()
        provider = ExotelProvider(sid="", api_key="", api_token="", client=client)

        with pytest.raises(ExternalServiceError) as exc:
            await provider.initiate_call(call_request())

        assert "EXOTEL" in str(exc.value)
        assert seen == []  # nothing was dialled


# --------------------------------------------------------------------------- #
# Exotel — hangup and numbers
# --------------------------------------------------------------------------- #
class TestExotelHangup:
    async def test_marks_the_call_completed_at_the_provider(self):
        provider, seen = exotel(httpx.Response(200, json={"Call": {"Sid": "exo-1"}}))

        assert await provider.hangup("exo-1") is True
        assert str(seen[0].url).endswith("/Calls/exo-1.json")
        assert form_of(seen[0])["Status"] == "completed"

    async def test_a_provider_error_propagates(self):
        provider, _ = exotel(httpx.Response(404, text="no such call"))

        with pytest.raises(ExternalServiceError):
            await provider.hangup("exo-gone")


class TestExotelProvisionNumber:
    INVENTORY = {
        "IncomingPhoneNumbers": [
            {"PhoneNumber": "+918040001111", "Sid": "num-1", "Region": "Karnataka"},
            {"PhoneNumber": "+912240002222", "Sid": "num-2", "Region": "Maharashtra"},
        ]
    }

    async def test_takes_the_first_number_when_nothing_is_specified(self):
        provider, _ = exotel(httpx.Response(200, json=self.INVENTORY))

        number = await provider.provision_number()

        assert number.number == "+918040001111"
        assert number.provider_number_id == "num-1"

    async def test_filters_by_region(self):
        provider, _ = exotel(httpx.Response(200, json=self.INVENTORY))

        number = await provider.provision_number(region="Maharashtra")

        assert number.number == "+912240002222"

    async def test_filters_by_exact_number(self):
        provider, _ = exotel(httpx.Response(200, json=self.INVENTORY))

        number = await provider.provision_number(number="+912240002222")

        assert number.provider_number_id == "num-2"

    async def test_no_match_is_an_external_service_error(self):
        provider, _ = exotel(httpx.Response(200, json=self.INVENTORY))

        with pytest.raises(ExternalServiceError) as exc:
            await provider.provision_number(region="Kerala")

        assert exc.value.details["region"] == "Kerala"

    async def test_an_empty_account_is_an_external_service_error(self):
        provider, _ = exotel(httpx.Response(200, json={"IncomingPhoneNumbers": []}))

        with pytest.raises(ExternalServiceError):
            await provider.provision_number()

    async def test_an_inventory_row_without_a_number_is_an_external_service_error(self):
        """Indexing a field the provider did not send raised `KeyError`.

        Provisioning is wrapped by callers that translate `ExternalServiceError`
        into a clean 502; a bare `KeyError` became an unhandled 500.
        """
        provider, _ = exotel(httpx.Response(200, json={"IncomingPhoneNumbers": [{"Sid": "num-1"}]}))

        with pytest.raises(ExternalServiceError):
            await provider.provision_number()

    async def test_release_is_a_no_op_that_reports_success(self):
        """Exotel numbers are released through their dashboard, not the API."""
        provider, seen = exotel()

        assert await provider.release_number("num-1") is True
        assert seen == []


# --------------------------------------------------------------------------- #
# Exotel — webhook parsing
# --------------------------------------------------------------------------- #
class TestExotelWebhook:
    def parse(self, payload: dict):
        provider, _ = exotel()
        return provider.parse_webhook(payload)

    async def test_parses_a_completed_callback(self):
        event = self.parse(
            {
                "CallSid": "exo-1",
                "Status": "completed",
                "ConversationDuration": "97",
                "CustomField": "our-call-id",
                "From": "+919999988888",
                "To": "+918000000001",
                "RecordingUrl": "https://recordings.exotel.test/exo-1.mp3",
            }
        )

        assert event.provider_call_id == "exo-1"
        assert event.status == CallStatus.COMPLETED
        assert event.duration_sec == 97
        assert event.call_id == "our-call-id"
        assert event.recording_url == "https://recordings.exotel.test/exo-1.mp3"

    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("queued", CallStatus.QUEUED),
            ("ringing", CallStatus.RINGING),
            ("in-progress", CallStatus.IN_PROGRESS),
            ("completed", CallStatus.COMPLETED),
            ("no-answer", CallStatus.NO_ANSWER),
            ("busy", CallStatus.BUSY),
            ("failed", CallStatus.FAILED),
            ("canceled", CallStatus.CANCELLED),
        ],
    )
    async def test_maps_every_documented_status(self, raw: str, expected: CallStatus):
        assert self.parse({"CallSid": "exo-1", "Status": raw}).status == expected

    async def test_status_casing_is_ignored(self):
        event = self.parse({"CallSid": "exo-1", "Status": "COMPLETED"})

        assert event.status == CallStatus.COMPLETED

    async def test_the_legacy_call_status_field_is_accepted(self):
        assert self.parse({"CallSid": "exo-1", "CallStatus": "busy"}).status == CallStatus.BUSY

    async def test_a_callback_with_no_status_carries_none(self):
        """A recording-ready ping has no status, and used to parse as `failed`.

        `failed` is terminal, so applying it ended a live call, stamped it
        unresolved and metered it for billing. An event that says nothing about
        the call's state must not be allowed to decide it.
        """
        event = self.parse({"CallSid": "exo-1", "RecordingUrl": "https://rec.test/1.mp3"})

        assert event.status is None
        assert event.recording_url == "https://rec.test/1.mp3"

    async def test_an_unrecognised_status_carries_none(self):
        """Exotel adding a status we have never seen must not end the call."""
        assert self.parse({"CallSid": "exo-1", "Status": "post-processing"}).status is None

    async def test_a_fractional_duration_is_accepted(self):
        """Exotel form bodies are strings, and durations arrive as `"45.0"`.

        `int("45.0")` raises, and `parse_webhook` runs inside the request
        handler, so this was a 500 — which makes Exotel redeliver the same body
        indefinitely.
        """
        assert self.parse({"CallSid": "exo-1", "ConversationDuration": "45.0"}).duration_sec == 45

    async def test_a_junk_duration_falls_back_to_zero(self):
        assert self.parse({"CallSid": "exo-1", "ConversationDuration": "n/a"}).duration_sec == 0

    async def test_an_empty_duration_falls_back_to_the_dial_duration(self):
        event = self.parse(
            {"CallSid": "exo-1", "ConversationDuration": "", "DialCallDuration": "31"}
        )

        assert event.duration_sec == 31

    async def test_a_negative_duration_is_floored_at_zero(self):
        assert self.parse({"CallSid": "exo-1", "ConversationDuration": "-5"}).duration_sec == 0

    async def test_a_numeric_error_code_is_coerced_to_text(self):
        """`call_service` slices `error_code`, so an int raised `TypeError`."""
        event = self.parse({"CallSid": "exo-1", "Status": "failed", "ErrorCode": 403})

        assert event.error_code == "403"

    async def test_absent_error_fields_stay_none(self):
        event = self.parse({"CallSid": "exo-1", "Status": "completed"})

        assert event.error_code is None
        assert event.error_message is None

    async def test_the_alternate_number_fields_are_read(self):
        event = self.parse(
            {"CallSid": "exo-1", "CallFrom": "+919999988888", "CallTo": "+918000001"}
        )

        assert event.from_number == "+919999988888"
        assert event.to_number == "+918000001"

    async def test_an_empty_payload_does_not_raise(self):
        """Providers send keepalives and probes; none of them may 500."""
        event = self.parse({})

        assert event.provider_call_id == ""
        assert event.status is None

    async def test_the_raw_payload_is_retained(self):
        payload = {"CallSid": "exo-1", "Status": "completed", "Unexpected": "kept"}

        assert self.parse(payload).raw == payload


class TestExotelRecording:
    async def test_downloads_the_bytes(self):
        provider, seen = exotel(httpx.Response(200, content=b"ID3-audio"))

        assert await provider.fetch_recording("https://rec.exotel.test/1.mp3") == b"ID3-audio"
        assert str(seen[0].url) == "https://rec.exotel.test/1.mp3"

    async def test_a_missing_recording_is_an_external_service_error(self):
        provider, _ = exotel(httpx.Response(404, text="gone"))

        with pytest.raises(ExternalServiceError):
            await provider.fetch_recording("https://rec.exotel.test/1.mp3")

    async def test_a_network_failure_is_an_external_service_error(self):
        provider, _ = exotel(httpx.ConnectError("reset"))

        with pytest.raises(ExternalServiceError):
            await provider.fetch_recording("https://rec.exotel.test/1.mp3")


# --------------------------------------------------------------------------- #
# Knowlarity
# --------------------------------------------------------------------------- #
class TestKnowlarityInitiateCall:
    async def test_posts_the_customer_and_agent_legs(self):
        provider, seen = knowlarity(
            httpx.Response(200, json={"success": {"call_id": "kn-1", "status": "queued"}})
        )

        await provider.initiate_call(call_request())

        payload = jsonlib.loads(seen[0].content)
        assert payload["customer_number"] == "+919999988888"
        assert payload["agent_number"] == "+918000000001"
        assert payload["additional_params"]["call_id"] == "11111111-2222-3333-4444-555555555555"

    async def test_posts_to_the_makecall_endpoint(self):
        provider, seen = knowlarity(
            httpx.Response(200, json={"success": {"call_id": "kn-1", "status": "queued"}})
        )

        await provider.initiate_call(call_request())

        assert str(seen[0].url) == "https://kpi.knowlarity.com/Basic/v1/account/call/makecall"

    async def test_returns_the_provider_call_id_and_status(self):
        provider, _ = knowlarity(
            httpx.Response(200, json={"success": {"call_id": "kn-7", "status": "ringing"}})
        )

        result = await provider.initiate_call(call_request())

        assert result.provider_call_id == "kn-7"
        assert result.status == CallStatus.RINGING

    async def test_a_top_level_call_id_is_accepted(self):
        provider, _ = knowlarity(httpx.Response(200, json={"call_id": "kn-8"}))

        assert (await provider.initiate_call(call_request())).provider_call_id == "kn-8"

    async def test_missing_credentials_are_reported_as_configuration(self):
        client, seen = make_client()
        provider = KnowlarityProvider(api_key="", sr_number="", client=client)

        with pytest.raises(ExternalServiceError) as exc:
            await provider.initiate_call(call_request())

        assert "KNOWLARITY" in str(exc.value)
        assert seen == []

    async def test_a_non_json_200_becomes_an_external_service_error(self):
        provider, _ = knowlarity(httpx.Response(200, text="<html>502</html>"))

        with pytest.raises(ExternalServiceError):
            await provider.initiate_call(call_request())

    async def test_a_4xx_becomes_an_external_service_error(self):
        provider, _ = knowlarity(httpx.Response(401, text="bad key"))

        with pytest.raises(ExternalServiceError):
            await provider.initiate_call(call_request())

    async def test_a_network_failure_becomes_an_external_service_error(self):
        provider, _ = knowlarity(httpx.ConnectError("reset"))

        with pytest.raises(ExternalServiceError):
            await provider.initiate_call(call_request())


class TestKnowlarityNumbers:
    INVENTORY = {
        "objects": [
            {"number": "+918040001111", "id": 11, "circle": "Karnataka"},
            {"number": "+912240002222", "id": 22, "circle": "Maharashtra"},
        ]
    }

    async def test_filters_by_circle(self):
        provider, _ = knowlarity(httpx.Response(200, json=self.INVENTORY))

        number = await provider.provision_number(region="Maharashtra")

        assert number.number == "+912240002222"
        assert number.provider_number_id == "22"

    async def test_the_legacy_numbers_key_is_accepted(self):
        provider, _ = knowlarity(
            httpx.Response(200, json={"numbers": [{"number": "+918040001111", "id": 11}]})
        )

        assert (await provider.provision_number()).number == "+918040001111"

    async def test_no_match_is_an_external_service_error(self):
        provider, _ = knowlarity(httpx.Response(200, json=self.INVENTORY))

        with pytest.raises(ExternalServiceError):
            await provider.provision_number(region="Kerala")

    async def test_an_inventory_row_without_a_number_is_an_external_service_error(self):
        provider, _ = knowlarity(httpx.Response(200, json={"objects": [{"id": 11}]}))

        with pytest.raises(ExternalServiceError):
            await provider.provision_number()

    async def test_release_calls_the_provider(self):
        provider, seen = knowlarity(httpx.Response(200, json={}))

        assert await provider.release_number("22") is True
        assert seen[0].method == "DELETE"
        assert str(seen[0].url).endswith("/numbers/22")


class TestKnowlarityWebhook:
    def parse(self, payload: dict):
        provider, _ = knowlarity()
        return provider.parse_webhook(payload)

    async def test_parses_a_completed_callback(self):
        event = self.parse(
            {
                "call_id": "kn-1",
                "call_status": "completed",
                "call_duration": 88,
                "additional_params": {"call_id": "our-call-id"},
                "resource_url": "https://rec.knowlarity.test/kn-1.mp3",
            }
        )

        assert event.provider_call_id == "kn-1"
        assert event.status == CallStatus.COMPLETED
        assert event.duration_sec == 88
        assert event.call_id == "our-call-id"

    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("initiated", CallStatus.QUEUED),
            ("ringing", CallStatus.RINGING),
            ("answered", CallStatus.IN_PROGRESS),
            ("connected", CallStatus.IN_PROGRESS),
            ("completed", CallStatus.COMPLETED),
            ("missed", CallStatus.NO_ANSWER),
            ("noanswer", CallStatus.NO_ANSWER),
            ("busy", CallStatus.BUSY),
            ("failed", CallStatus.FAILED),
            ("cancelled", CallStatus.CANCELLED),
        ],
    )
    async def test_maps_every_documented_status(self, raw: str, expected: CallStatus):
        assert self.parse({"call_id": "kn-1", "call_status": raw}).status == expected

    async def test_an_unrecognised_status_carries_none(self):
        assert self.parse({"call_id": "kn-1", "call_status": "queued_at_carrier"}).status is None

    async def test_a_callback_with_no_status_carries_none(self):
        assert self.parse({"call_id": "kn-1"}).status is None

    async def test_a_fractional_duration_is_accepted(self):
        assert self.parse({"call_id": "kn-1", "call_duration": 45.6}).duration_sec == 45

    async def test_a_junk_duration_falls_back_to_zero(self):
        assert self.parse({"call_id": "kn-1", "call_duration": "unknown"}).duration_sec == 0

    async def test_a_numeric_error_code_is_coerced_to_text(self):
        event = self.parse({"call_id": "kn-1", "call_status": "failed", "error_code": 500})

        assert event.error_code == "500"

    async def test_additional_params_may_be_missing(self):
        assert self.parse({"call_id": "kn-1", "additional_params": None}).call_id is None

    async def test_an_empty_payload_does_not_raise(self):
        event = self.parse({})

        assert event.provider_call_id == ""
        assert event.status is None


class TestKnowlarityRecording:
    async def test_downloads_the_bytes(self):
        provider, _ = knowlarity(httpx.Response(200, content=b"ID3-audio"))

        assert await provider.fetch_recording("https://rec.test/1.mp3") == b"ID3-audio"

    async def test_a_missing_recording_is_an_external_service_error(self):
        provider, _ = knowlarity(httpx.Response(404, text="gone"))

        with pytest.raises(ExternalServiceError):
            await provider.fetch_recording("https://rec.test/1.mp3")


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #
class TestProviderRegistry:
    @pytest.fixture(autouse=True)
    def no_override(self):
        """The suite-wide mock override hides the real resolution path."""
        telephony.set_provider_override(None)
        yield
        telephony.reset_providers()

    def test_resolves_each_provider_by_name(self):
        assert isinstance(telephony.get_provider("exotel"), ExotelProvider)
        assert isinstance(telephony.get_provider("knowlarity"), KnowlarityProvider)
        assert isinstance(telephony.get_provider("mock"), MockTelephonyProvider)

    def test_the_name_is_case_insensitive(self):
        assert isinstance(telephony.get_provider("ExOtEl"), ExotelProvider)

    def test_an_unknown_name_falls_back_to_the_mock(self):
        """Better a provider that does nothing than a crash on the call path."""
        assert isinstance(telephony.get_provider("carrier-pigeon"), MockTelephonyProvider)

    def test_providers_are_cached_per_name(self):
        assert telephony.get_provider("exotel") is telephony.get_provider("exotel")

    def test_an_override_wins_over_the_configured_provider(self):
        override = MockTelephonyProvider()
        telephony.set_provider_override(override)

        assert telephony.get_provider("exotel") is override

    def test_reset_clears_the_override_and_the_cache(self):
        first = telephony.get_provider("exotel")
        telephony.set_provider_override(MockTelephonyProvider())
        telephony.reset_providers()

        assert telephony.get_provider("exotel") is not first
