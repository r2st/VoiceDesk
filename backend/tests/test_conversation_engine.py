"""The conversation engine: flow traversal, LLM fallback and post-call rollups.

``test_voice_booking.py`` and ``test_leads.py`` already cover the
``book_appointment`` and ``qualify_lead`` nodes end to end; this file covers
the rest of the engine's surface — opt-out, loop protection, the node types
those files don't exercise (``intent_branch``, ``condition``, ``api_call``,
``handoff``, ``transfer``), the pure LLM fallback path, and the module-level
helpers that support them.
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.business import Business
from app.models.call import CallLog, DNDRegistry
from app.models.enums import CallResolution, HandoffReason, Language
from app.models.voice_agent import VoiceAgent
from app.schemas.agent import IntentCreate
from app.schemas.appointment import AppointmentCreate
from app.services import agent_service, appointment_service
from app.services.conversation_engine import (
    _allowed_languages,
    _evaluate_condition,
    _json_path,
    _render_string,
    _render_template,
    _spoken_time,
    _system_prompt,
    get_engine,
)
from app.services.flow import ConditionNode
from tests.fakes import FakeLLMClient

IST = ZoneInfo("Asia/Kolkata")


def _flow(*nodes: dict, start: str | None = None) -> dict:
    return {"start_node": start or nodes[0]["id"], "nodes": list(nodes)}


def _land_on(call: CallLog, node_id: str, variables: dict | None = None) -> None:
    """Point the call's saved state straight at ``node_id``, as if earlier
    turns had already walked it there."""
    call.metadata_json = {
        "state": {
            "turn_index": 1,
            "current_node": node_id,
            "variables": variables or {},
        }
    }


# --------------------------------------------------------------------------- #
# start_call
# --------------------------------------------------------------------------- #
class TestStartCall:
    async def test_a_start_node_that_is_not_a_message_is_not_spoken(
        self, session: AsyncSession, call: CallLog, agent: VoiceAgent
    ) -> None:
        """The flow's own opening line is only used when the start node is a
        ``message`` — a flow that starts on a ``collect`` node relies entirely
        on the agent's configured greeting."""
        agent.recording_enabled = False
        agent.flow_json = _flow(
            {
                "id": "ask",
                "type": "collect",
                "prompt": "What is your name?",
                "variable": "name",
                "next": "bye",
            },
            {"id": "bye", "type": "end", "text": "Bye"},
        )
        await session.flush()

        result = await get_engine().start_call(session, call, agent)

        assert result.reply == agent.greeting
        assert result.node_id == "ask"
        assert call.metadata_json["state"]["current_node"] == "ask"


# --------------------------------------------------------------------------- #
# Opt-out (TRAI 8.2.5)
# --------------------------------------------------------------------------- #
class TestOptOutDuringACall:
    async def test_an_opt_out_utterance_ends_the_call_and_registers_dnd(
        self, session: AsyncSession, business: Business, call: CallLog, agent: VoiceAgent
    ) -> None:
        result = await get_engine().process_turn(session, call, agent, "please stop calling me")

        assert result.should_end_call is True
        assert result.disposition is CallResolution.RESOLVED
        assert call.opted_out is True

        row = (
            await session.execute(
                select(DNDRegistry).where(
                    DNDRegistry.business_id == business.id,
                    DNDRegistry.phone_number == call.caller_number,
                )
            )
        ).scalar_one()
        assert row.is_dnd is True

    async def test_the_opt_out_reply_is_localised(
        self, session: AsyncSession, call: CallLog, agent: VoiceAgent
    ) -> None:
        agent.language = Language.ENGLISH
        await session.flush()

        result = await get_engine().process_turn(session, call, agent, "I want to opt out")

        assert "removed" in result.reply.lower()


# --------------------------------------------------------------------------- #
# Flow-loop protection
# --------------------------------------------------------------------------- #
class TestFlowLoopDetection:
    async def test_a_cycle_between_message_nodes_stops_instead_of_looping_forever(
        self, session: AsyncSession, call: CallLog, agent: VoiceAgent
    ) -> None:
        agent.flow_json = _flow(
            {"id": "a", "type": "message", "text": "First.", "next": "b"},
            {"id": "b", "type": "message", "text": "Second.", "next": "a"},
        )
        await session.flush()
        _land_on(call, "a")
        await session.flush()

        result = await get_engine().process_turn(session, call, agent, "hello")

        assert result.reply == "First. Second."
        assert result.node_id == "a"


# --------------------------------------------------------------------------- #
# intent_branch
# --------------------------------------------------------------------------- #
class TestIntentBranchNode:
    async def test_a_matched_intent_takes_its_branch(
        self, session: AsyncSession, business: Business, call: CallLog, agent: VoiceAgent
    ) -> None:
        await agent_service.create_intent(
            session,
            business.id,
            IntentCreate(name="check_status", sample_phrases=["order status"]),
        )
        agent.flow_json = _flow(
            {
                "id": "route",
                "type": "intent_branch",
                "branches": {"check_status": "status_node"},
                "default": "fallback",
            },
            {"id": "status_node", "type": "message", "text": "Checking your order."},
            {"id": "fallback", "type": "message", "text": "I did not follow that."},
        )
        await session.flush()
        _land_on(call, "route")
        await session.flush()

        result = await get_engine().process_turn(session, call, agent, "what is my order status")

        assert result.reply == "Checking your order."
        assert result.detected_intent == "check_status"

    async def test_no_match_and_no_default_falls_through_to_the_llm(
        self, session: AsyncSession, call: CallLog, agent: VoiceAgent, fake_llm: FakeLLMClient
    ) -> None:
        agent.flow_json = _flow(
            {
                "id": "route",
                "type": "intent_branch",
                "branches": {"unrelated": "somewhere"},
                "default": None,
            },
            {"id": "somewhere", "type": "message", "text": "unreachable"},
        )
        await session.flush()
        _land_on(call, "route")
        await session.flush()
        fake_llm.replies.append("Let me see how I can help with that.")

        result = await get_engine().process_turn(session, call, agent, "tell me a joke")

        assert result.reply == "Let me see how I can help with that."


# --------------------------------------------------------------------------- #
# condition
# --------------------------------------------------------------------------- #
class TestConditionNode:
    async def test_a_matching_condition_takes_if_true(
        self, session: AsyncSession, call: CallLog, agent: VoiceAgent
    ) -> None:
        agent.flow_json = _flow(
            {
                "id": "check",
                "type": "condition",
                "variable": "amount",
                "operator": "gt",
                "value": 100,
                "if_true": "high",
                "if_false": "low",
            },
            {"id": "high", "type": "message", "text": "High value caller."},
            {"id": "low", "type": "message", "text": "Standard caller."},
        )
        await session.flush()
        _land_on(call, "check", variables={"amount": "150"})
        await session.flush()

        result = await get_engine().process_turn(session, call, agent, "hi")

        assert result.reply == "High value caller."

    async def test_an_unmatched_condition_with_no_route_falls_through_to_the_llm(
        self, session: AsyncSession, call: CallLog, agent: VoiceAgent, fake_llm: FakeLLMClient
    ) -> None:
        agent.flow_json = _flow(
            {
                "id": "check",
                "type": "condition",
                "variable": "amount",
                "operator": "gt",
                "value": 100,
                "if_true": "high",
                "if_false": None,
            },
            {"id": "high", "type": "message", "text": "High value caller."},
        )
        await session.flush()
        _land_on(call, "check", variables={"amount": "10"})
        await session.flush()
        fake_llm.replies.append("How can I help?")

        result = await get_engine().process_turn(session, call, agent, "hi")

        assert result.reply == "How can I help?"


# --------------------------------------------------------------------------- #
# api_call
# --------------------------------------------------------------------------- #
class _FakeHTTPResponse:
    def __init__(self, status_code: int, payload: dict | None = None) -> None:
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.content = b"{}"

    def json(self) -> dict:
        return self._payload


class TestApiCallNode:
    def _api_flow(self, **node_overrides: object) -> dict:
        node = {
            "id": "lookup",
            "type": "api_call",
            "url": "https://api.example.test/lookup",
            "method": "POST",
            "body_template": {"customer": "{{caller_name}}"},
            "save_as": {"ticket_id": "$.data.id"},
            "next": "confirm",
            "on_error": "fail",
            **node_overrides,
        }
        return _flow(
            node,
            {"id": "confirm", "type": "message", "text": "Ticket created."},
            {"id": "fail", "type": "message", "text": "Something went wrong."},
        )

    async def test_a_successful_call_saves_the_response_and_continues(
        self, session: AsyncSession, call: CallLog, agent: VoiceAgent, monkeypatch
    ) -> None:
        captured: dict = {}

        async def fake_request(self, method, url, *, json=None, params=None, headers=None):
            captured.update(method=method, url=url, json=json, params=params)
            return _FakeHTTPResponse(200, {"data": {"id": "abc123"}})

        monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)
        agent.flow_json = self._api_flow()
        await session.flush()
        _land_on(call, "lookup", variables={"caller_name": "Asha"})
        await session.flush()

        result = await get_engine().process_turn(session, call, agent, "hi")

        assert result.reply == "Ticket created."
        assert captured["method"] == "POST"
        assert captured["json"] == {"customer": "Asha"}
        assert captured["params"] is None
        assert call.metadata_json["state"]["variables"]["ticket_id"] == "abc123"

    async def test_a_get_request_sends_the_body_as_query_params(
        self, session: AsyncSession, call: CallLog, agent: VoiceAgent, monkeypatch
    ) -> None:
        captured: dict = {}

        async def fake_request(self, method, url, *, json=None, params=None, headers=None):
            captured.update(method=method, json=json, params=params)
            return _FakeHTTPResponse(200, {})

        monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)
        agent.flow_json = self._api_flow(method="GET", save_as={})
        await session.flush()
        _land_on(call, "lookup", variables={"caller_name": "Asha"})
        await session.flush()

        await get_engine().process_turn(session, call, agent, "hi")

        assert captured["method"] == "GET"
        assert captured["json"] is None
        assert captured["params"] == {"customer": "Asha"}

    async def test_an_error_status_takes_the_on_error_route(
        self, session: AsyncSession, call: CallLog, agent: VoiceAgent, monkeypatch
    ) -> None:
        async def fake_request(self, method, url, **kwargs):
            return _FakeHTTPResponse(500)

        monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)
        agent.flow_json = self._api_flow()
        await session.flush()
        _land_on(call, "lookup", variables={"caller_name": "Asha"})
        await session.flush()

        result = await get_engine().process_turn(session, call, agent, "hi")

        assert result.reply == "Something went wrong."

    async def test_a_network_failure_takes_the_on_error_route(
        self, session: AsyncSession, call: CallLog, agent: VoiceAgent, monkeypatch
    ) -> None:
        async def fake_request(self, method, url, **kwargs):
            raise httpx.ConnectError("boom")

        monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)
        agent.flow_json = self._api_flow()
        await session.flush()
        _land_on(call, "lookup", variables={"caller_name": "Asha"})
        await session.flush()

        result = await get_engine().process_turn(session, call, agent, "hi")

        assert result.reply == "Something went wrong."

    async def test_no_next_node_falls_through_to_the_llm(
        self,
        session: AsyncSession,
        call: CallLog,
        agent: VoiceAgent,
        monkeypatch,
        fake_llm: FakeLLMClient,
    ) -> None:
        async def fake_request(self, method, url, **kwargs):
            return _FakeHTTPResponse(200, {})

        monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)
        agent.flow_json = _flow(
            {
                "id": "lookup",
                "type": "api_call",
                "url": "https://api.example.test/lookup",
                "method": "POST",
                "next": None,
            }
        )
        await session.flush()
        _land_on(call, "lookup", variables={"caller_name": "Asha"})
        await session.flush()
        fake_llm.replies.append("Anything else I can help with?")

        result = await get_engine().process_turn(session, call, agent, "hi")

        assert result.reply == "Anything else I can help with?"


# --------------------------------------------------------------------------- #
# handoff / transfer flow nodes
# --------------------------------------------------------------------------- #
class TestHandoffFlowNode:
    async def test_the_default_message_ends_the_call_when_there_is_no_next_node(
        self, session: AsyncSession, call: CallLog, agent: VoiceAgent
    ) -> None:
        agent.language = Language.ENGLISH
        agent.flow_json = _flow({"id": "hand", "type": "handoff", "channel": "whatsapp"})
        await session.flush()
        _land_on(call, "hand")
        await session.flush()

        result = await get_engine().process_turn(session, call, agent, "send me the brochure")

        assert result.should_handoff is True
        assert result.handoff_reason is HandoffReason.UNSUPPORTED_INTENT
        assert result.should_end_call is True
        assert result.disposition is CallResolution.HANDED_OFF
        assert "WhatsApp" in result.reply

    async def test_a_custom_message_with_a_next_node_keeps_the_call_open(
        self, session: AsyncSession, call: CallLog, agent: VoiceAgent
    ) -> None:
        agent.flow_json = _flow(
            {
                "id": "hand",
                "type": "handoff",
                "channel": "whatsapp",
                "message": "Sending it over now.",
                "next": "bye",
            },
            {"id": "bye", "type": "message", "text": "Anything else?"},
        )
        await session.flush()
        _land_on(call, "hand")
        await session.flush()

        result = await get_engine().process_turn(session, call, agent, "send me the brochure")

        assert result.reply == "Sending it over now."
        assert result.should_end_call is False


class TestTransferFlowNode:
    async def test_an_announcement_is_spoken_before_the_transfer(
        self, session: AsyncSession, call: CallLog, agent: VoiceAgent
    ) -> None:
        agent.flow_json = _flow(
            {
                "id": "xfer",
                "type": "transfer",
                "to_number": "+919000000000",
                "announcement": "One moment, connecting you.",
            }
        )
        await session.flush()
        _land_on(call, "xfer")
        await session.flush()

        result = await get_engine().process_turn(session, call, agent, "let me talk to a person")

        assert result.reply == "One moment, connecting you."
        assert result.transfer_to == "+919000000000"
        assert result.should_end_call is True
        assert result.disposition is CallResolution.ESCALATED
        assert call.metadata_json["state"]["current_node"] is None

    async def test_no_announcement_uses_the_default_line(
        self, session: AsyncSession, call: CallLog, agent: VoiceAgent
    ) -> None:
        agent.flow_json = _flow({"id": "xfer", "type": "transfer", "to_number": "+919000000000"})
        await session.flush()
        _land_on(call, "xfer")
        await session.flush()

        result = await get_engine().process_turn(session, call, agent, "let me talk to a person")

        assert result.reply == "Connecting you to a colleague now."


# --------------------------------------------------------------------------- #
# Pure LLM fallback (no flow, or a flow that ran dry)
# --------------------------------------------------------------------------- #
class TestLlmReply:
    async def test_a_flowless_agent_talks_purely_through_the_llm(
        self, session: AsyncSession, call: CallLog, agent: VoiceAgent, fake_llm: FakeLLMClient
    ) -> None:
        agent.flow_json = {}
        await session.flush()
        fake_llm.replies.append("Sure, tell me more.")

        result = await get_engine().process_turn(session, call, agent, "I have a question")

        assert result.reply == "Sure, tell me more."
        assert result.model_used == fake_llm.model

    async def test_an_llm_failure_falls_back_to_the_agents_configured_message(
        self, session: AsyncSession, call: CallLog, agent: VoiceAgent, fake_llm: FakeLLMClient
    ) -> None:
        agent.flow_json = {}
        agent.fallback_message = "Sorry, please try again shortly."
        agent.whatsapp_handoff_enabled = False
        fake_llm.raise_error = True
        await session.flush()

        result = await get_engine().process_turn(session, call, agent, "I have a question")

        assert result.reply == "Sorry, please try again shortly."
        assert result.confidence == 0.3

    async def test_an_llm_failure_without_a_configured_fallback_uses_the_localised_default(
        self, session: AsyncSession, call: CallLog, agent: VoiceAgent, fake_llm: FakeLLMClient
    ) -> None:
        agent.flow_json = {}
        agent.fallback_message = None
        agent.language = Language.ENGLISH
        agent.whatsapp_handoff_enabled = False
        fake_llm.raise_error = True
        await session.flush()

        result = await get_engine().process_turn(session, call, agent, "I have a question")

        assert "trouble understanding" in result.reply.lower()


# --------------------------------------------------------------------------- #
# Appointment booking: the alternatives-offer edge cases
# --------------------------------------------------------------------------- #
class TestOfferAlternatives:
    def _booking_flow(self, **node_overrides: object) -> dict:
        node = {
            "id": "book",
            "type": "book_appointment",
            "name_variable": "customer_name",
            "time_variable": "preferred_time",
            "offer_alternatives": 2,
            "next": "wrap_up",
            "on_unavailable": "retry",
            **node_overrides,
        }
        return _flow(
            node,
            {"id": "wrap_up", "type": "end", "text": "Booked."},
            {"id": "retry", "type": "message", "text": "Let's try again."},
        )

    async def test_zero_alternatives_configured_gives_a_flat_apology(
        self, session: AsyncSession, business: Business, call: CallLog, agent: VoiceAgent
    ) -> None:
        wanted = datetime.now(IST).replace(hour=11, minute=0, second=0, microsecond=0)
        from datetime import timedelta

        wanted += timedelta(days=1)
        while wanted.weekday() == 6:
            wanted += timedelta(days=1)
        await appointment_service.book(
            session,
            business.id,
            AppointmentCreate(
                customer_name="Existing Caller", customer_phone="+919800000001", scheduled_at=wanted
            ),
        )
        agent.flow_json = self._booking_flow(offer_alternatives=0)
        await session.flush()
        phrase = f"{wanted:%A} at {wanted:%H}:00"
        _land_on(
            call, "book", variables={"customer_name": "Asha", "preferred_time": phrase}
        )
        await session.flush()

        result = await get_engine().process_turn(session, call, agent, phrase)

        assert "not available" in result.reply.lower()

    async def test_no_open_slots_left_says_so_instead_of_listing_none(
        self,
        session: AsyncSession,
        business: Business,
        call: CallLog,
        agent: VoiceAgent,
        monkeypatch,
    ) -> None:
        from datetime import timedelta

        wanted = datetime.now(IST).replace(hour=11, minute=0, second=0, microsecond=0)
        wanted += timedelta(days=1)
        while wanted.weekday() == 6:
            wanted += timedelta(days=1)
        await appointment_service.book(
            session,
            business.id,
            AppointmentCreate(
                customer_name="Existing Caller", customer_phone="+919800000001", scheduled_at=wanted
            ),
        )
        monkeypatch.setattr(appointment_service, "next_available_slots", _no_slots)
        agent.flow_json = self._booking_flow(offer_alternatives=2)
        await session.flush()
        phrase = f"{wanted:%A} at {wanted:%H}:00"
        _land_on(
            call, "book", variables={"customer_name": "Asha", "preferred_time": phrase}
        )
        await session.flush()

        result = await get_engine().process_turn(session, call, agent, phrase)

        assert "nothing free" in result.reply.lower()


async def _no_slots(*args, **kwargs) -> list:
    return []


# --------------------------------------------------------------------------- #
# Post-call summary
# --------------------------------------------------------------------------- #
class TestSummariseCallLlmFailure:
    async def test_a_summary_failure_falls_back_to_a_turn_count(
        self, session: AsyncSession, call: CallLog, agent: VoiceAgent, fake_llm: FakeLLMClient
    ) -> None:
        engine = get_engine()
        await engine.process_turn(session, call, agent, "Namaste")
        fake_llm.raise_error = True

        summary, sentiment, average = await engine.summarise_call(session, call)

        assert "unavailable" in summary.lower()
        assert call.summary == summary


# --------------------------------------------------------------------------- #
# Pure module helpers
# --------------------------------------------------------------------------- #
class TestAllowedLanguages:
    def test_an_unrecognised_supported_language_code_is_skipped(self) -> None:
        fake_agent = VoiceAgent(language=Language.HINDI, supported_languages=["hi", "xx", "en"])

        allowed = _allowed_languages(fake_agent)

        assert allowed == [Language.HINDI, Language.ENGLISH]


class TestSystemPrompt:
    def test_gathered_variables_are_listed(self) -> None:
        from app.services.conversation_engine import CallState

        fake_agent = VoiceAgent(persona="You are helpful.")
        state = CallState(variables={"name": "Asha", "empty": ""})

        prompt = _system_prompt(fake_agent, Language.ENGLISH, state)

        assert "name=Asha" in prompt
        assert "empty" not in prompt.split("Information gathered so far:")[1]

    def test_no_variables_reads_as_none_yet(self) -> None:
        from app.services.conversation_engine import CallState

        fake_agent = VoiceAgent(persona="You are helpful.")
        prompt = _system_prompt(fake_agent, Language.ENGLISH, CallState())

        assert "none yet" in prompt


class TestSpokenTime:
    def test_hindi_uses_the_localised_am_pm_markers(self) -> None:
        when = datetime(2026, 3, 5, 9, 30, tzinfo=UTC)

        spoken = _spoken_time(when, "Asia/Kolkata", Language.HINDI)

        assert "AM" not in spoken and "PM" not in spoken
        assert "बजे" in spoken


class TestEvaluateCondition:
    def _node(self, **kwargs) -> ConditionNode:
        return ConditionNode(id="c", variable="v", **kwargs)

    def test_exists_is_true_only_when_the_variable_is_present(self) -> None:
        assert _evaluate_condition(self._node(operator="exists"), {"v": "x"}) is True
        assert _evaluate_condition(self._node(operator="exists"), {}) is False

    def test_a_missing_variable_never_matches_a_value_comparison(self) -> None:
        assert _evaluate_condition(self._node(operator="eq", value="x"), {}) is False

    def test_eq_and_neq_compare_as_strings(self) -> None:
        assert _evaluate_condition(self._node(operator="eq", value="5"), {"v": 5}) is True
        assert _evaluate_condition(self._node(operator="neq", value="5"), {"v": 5}) is False

    def test_contains_is_case_insensitive(self) -> None:
        assert _evaluate_condition(
            self._node(operator="contains", value="OPEN"), {"v": "shop is open now"}
        )

    @pytest.mark.parametrize(
        ("operator", "value", "actual", "expected"),
        [
            ("gt", 10, "20", True),
            ("gte", 20, "20", True),
            ("lt", 20, "10", True),
            ("lte", 10, "10", True),
            ("gt", 10, "5", False),
        ],
    )
    def test_numeric_comparisons(self, operator, value, actual, expected) -> None:
        node = self._node(operator=operator, value=value)
        assert _evaluate_condition(node, {"v": actual}) is expected

    def test_a_non_numeric_value_fails_a_numeric_comparison_instead_of_raising(self) -> None:
        node = self._node(operator="gt", value=10)
        assert _evaluate_condition(node, {"v": "not a number"}) is False

    def test_an_unknown_operator_matches_nothing(self) -> None:
        node = self._node(operator="frobnicate", value=1)
        assert _evaluate_condition(node, {"v": 1}) is False


class TestRenderTemplate:
    def test_placeholders_are_substituted(self) -> None:
        assert _render_string("Hello {{name}}", {"name": "Asha"}) == "Hello Asha"

    def test_an_unset_placeholder_is_left_as_is(self) -> None:
        assert _render_string("Hello {{name}}", {}) == "Hello {{name}}"

    def test_a_template_dict_renders_only_its_string_values(self) -> None:
        rendered = _render_template({"name": "{{name}}", "count": 3}, {"name": "Asha"})
        assert rendered == {"name": "Asha", "count": 3}

    def test_an_empty_template_renders_to_an_empty_dict(self) -> None:
        assert _render_template(None, {}) == {}


class TestJsonPath:
    def test_a_nested_dict_path_resolves(self) -> None:
        assert _json_path({"data": {"id": "abc"}}, "$.data.id") == "abc"

    def test_a_list_index_resolves(self) -> None:
        assert _json_path({"items": [{"id": "a"}, {"id": "b"}]}, "items.1.id") == "b"

    def test_an_out_of_range_index_resolves_to_none(self) -> None:
        assert _json_path({"items": [1]}, "items.5") is None

    def test_a_path_through_a_scalar_resolves_to_none(self) -> None:
        assert _json_path({"data": "leaf"}, "data.nested") is None

    def test_the_bare_root_prefix_returns_the_whole_payload(self) -> None:
        assert _json_path({"data": "leaf"}, "$.") == {"data": "leaf"}
