"""Voice-to-WhatsApp handoff (design doc §4.5).

The handoff is the escape hatch for a conversation the voice agent cannot
finish, so the properties that matter are that it happens at most once per
call, that a provider rejection is recorded rather than swallowed, and that a
recorded failure can still be retried.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.core.errors import NotFoundError
from app.models.call import CallLog, Conversation, WhatsAppHandoff
from app.models.enums import (
    CallDirection,
    CallResolution,
    CallStatus,
    HandoffReason,
    HandoffStatus,
    Language,
    SpeakerRole,
    TelephonyProvider,
)
from app.services import whatsapp


async def add_turns(session, call: CallLog, *turns: tuple[str, str]) -> None:
    """Append ``(role, content)`` turns to a call's transcript."""
    for index, (role, content) in enumerate(turns):
        session.add(
            Conversation(
                business_id=call.business_id,
                call_id=call.id,
                turn_index=index,
                role=role,
                content=content,
                language=Language.HINDI,
            )
        )
    await session.flush()


async def handoffs_for(session, call: CallLog) -> list[WhatsAppHandoff]:
    rows = await session.execute(
        select(WhatsAppHandoff)
        .where(WhatsAppHandoff.call_id == call.id)
        .order_by(WhatsAppHandoff.created_at)
    )
    return list(rows.scalars().all())


# --------------------------------------------------------------------------- #
# The decision to escalate
# --------------------------------------------------------------------------- #
class TestShouldHandoff:
    def test_a_disabled_agent_never_hands_off(self, agent):
        """The tenant's switch wins over every other signal, including a
        confidence of zero — otherwise turning the feature off would not."""
        agent.whatsapp_handoff_enabled = False

        assert whatsapp.should_handoff(confidence=0.0, agent=agent, utterance="whatsapp") is None

    def test_confidence_above_the_threshold_stays_on_the_call(self, agent):
        assert whatsapp.should_handoff(confidence=0.95, agent=agent, utterance="haan ji") is None

    def test_confidence_below_the_threshold_escalates(self, agent):
        agent.handoff_confidence_threshold = 0.70

        reason = whatsapp.should_handoff(confidence=0.69, agent=agent)

        assert reason is HandoffReason.LOW_CONFIDENCE

    def test_the_threshold_is_exclusive(self, agent):
        """Exactly at the threshold is still good enough to continue."""
        agent.handoff_confidence_threshold = 0.70

        assert whatsapp.should_handoff(confidence=0.70, agent=agent) is None

    def test_the_agent_threshold_is_honoured_over_the_default(self, agent):
        agent.handoff_confidence_threshold = 0.40

        # 0.5 would escalate under the 0.70 default but not under this agent's.
        assert whatsapp.should_handoff(confidence=0.50, agent=agent) is None

    def test_a_missing_threshold_falls_back_to_the_default(self, agent):
        agent.handoff_confidence_threshold = None

        assert whatsapp.should_handoff(confidence=0.69, agent=agent) is HandoffReason.LOW_CONFIDENCE
        assert whatsapp.should_handoff(confidence=0.71, agent=agent) is None

    @pytest.mark.parametrize(
        "utterance",
        [
            "please whatsapp me the details",
            "can you text me instead",
            "message me the address",
            "send me a message please",
            "मुझे व्हाट्सएप पर भेजिए",
        ],
    )
    def test_a_caller_asking_for_text_escalates(self, agent, utterance):
        reason = whatsapp.should_handoff(confidence=0.99, agent=agent, utterance=utterance)

        assert reason is HandoffReason.CALLER_REQUEST

    @pytest.mark.parametrize(
        "utterance",
        [
            "I need to send document copies",
            "can I share document with you",
            "where do I upload my report",
            "I will send a photo",
            "do you need the receipt",
            "please send the invoice",
        ],
    )
    def test_a_document_request_escalates(self, agent, utterance):
        reason = whatsapp.should_handoff(confidence=0.99, agent=agent, utterance=utterance)

        assert reason is HandoffReason.DOCUMENT_REQUIRED

    def test_the_utterance_match_is_case_insensitive(self, agent):
        reason = whatsapp.should_handoff(confidence=0.99, agent=agent, utterance="WhatsApp Me")

        assert reason is HandoffReason.CALLER_REQUEST

    def test_an_explicit_request_outranks_low_confidence(self, agent):
        """Both signals fire at once; the caller's own words are the better
        explanation, and the reason is what the dashboard shows a human."""
        reason = whatsapp.should_handoff(confidence=0.10, agent=agent, utterance="whatsapp me")

        assert reason is HandoffReason.CALLER_REQUEST

    def test_a_document_request_outranks_low_confidence(self, agent):
        reason = whatsapp.should_handoff(confidence=0.10, agent=agent, utterance="send document")

        assert reason is HandoffReason.DOCUMENT_REQUIRED

    def test_an_empty_utterance_is_judged_on_confidence_alone(self, agent):
        assert whatsapp.should_handoff(confidence=0.99, agent=agent, utterance="") is None
        assert whatsapp.should_handoff(confidence=0.10, agent=agent) is HandoffReason.LOW_CONFIDENCE


# --------------------------------------------------------------------------- #
# The live call path
# --------------------------------------------------------------------------- #
class TestEngineHandoffTriggers:
    """The engine must escalate on all three triggers in design doc §4.5.

    A plain turn scores 0.75, so a threshold of 0.5 rules the confidence
    trigger out and leaves only the caller's words as a possible cause.
    """

    async def turn(self, session, call, agent, utterance: str):
        from app.services.conversation_engine import get_engine

        await session.flush()
        return await get_engine().process_turn(session, call, agent, utterance)

    async def test_a_caller_asking_for_whatsapp_is_handed_off(self, session, call, agent):
        agent.handoff_confidence_threshold = 0.5

        result = await self.turn(session, call, agent, "please whatsapp me the details")

        assert result.should_handoff is True
        assert result.handoff_reason is HandoffReason.CALLER_REQUEST

    async def test_a_caller_needing_to_send_a_document_is_handed_off(self, session, call, agent):
        agent.handoff_confidence_threshold = 0.5

        result = await self.turn(session, call, agent, "I need to send document copies")

        assert result.should_handoff is True
        assert result.handoff_reason is HandoffReason.DOCUMENT_REQUIRED

    async def test_an_ordinary_turn_is_not_handed_off(self, session, call, agent):
        agent.handoff_confidence_threshold = 0.5

        result = await self.turn(session, call, agent, "Namaste, kaise ho")

        assert result.should_handoff is False
        assert result.handoff_reason is None

    async def test_a_confident_turn_below_the_threshold_is_handed_off(self, session, call, agent):
        agent.handoff_confidence_threshold = 1.0

        result = await self.turn(session, call, agent, "Namaste, kaise ho")

        assert result.should_handoff is True
        assert result.handoff_reason is HandoffReason.LOW_CONFIDENCE

    async def test_a_disabled_agent_stays_on_the_call(self, session, call, agent):
        """Even when the caller asks for WhatsApp by name."""
        agent.whatsapp_handoff_enabled = False
        agent.handoff_confidence_threshold = 1.0

        result = await self.turn(session, call, agent, "please whatsapp me the details")

        assert result.should_handoff is False
        assert result.handoff_reason is None

    async def test_the_spoken_reply_matches_the_reason(self, session, call, agent):
        """Apologising for not following a caller who simply asked to be
        texted reads as a malfunction, so the wording tracks the reason."""
        agent.handoff_confidence_threshold = 0.5
        agent.language = Language.ENGLISH
        agent.supported_languages = [Language.ENGLISH]

        requested = await self.turn(session, call, agent, "please text me the details")

        assert requested.reply == "Of course. I am sending you the details on WhatsApp."

    async def test_a_document_handoff_says_where_to_send_it(self, session, call, agent):
        agent.handoff_confidence_threshold = 0.5
        agent.language = Language.ENGLISH
        agent.supported_languages = [Language.ENGLISH]

        result = await self.turn(session, call, agent, "where do I upload my report")

        assert "share the document" in result.reply

    async def test_a_low_confidence_handoff_keeps_the_apology(self, session, call, agent):
        agent.handoff_confidence_threshold = 1.0
        agent.language = Language.ENGLISH
        agent.supported_languages = [Language.ENGLISH]

        result = await self.turn(session, call, agent, "mumble mumble")

        assert result.reply.startswith("I could not quite follow that.")


# --------------------------------------------------------------------------- #
# Sending the handoff
# --------------------------------------------------------------------------- #
class TestInitiateHandoff:
    async def test_sends_and_records_the_handoff(self, session, business, call, fake_whatsapp):
        handoff = await whatsapp.initiate_handoff(
            session, call, reason=HandoffReason.CALLER_REQUEST, summary="Blood test on Friday."
        )

        assert handoff.business_id == business.id
        assert handoff.call_id == call.id
        assert handoff.status == HandoffStatus.SENT
        assert handoff.provider == "fake"
        assert handoff.provider_message_id == "fake-wa-1"
        assert handoff.sent_at is not None
        assert handoff.error_message is None

        assert len(fake_whatsapp.sent) == 1
        assert "Blood test on Friday." in fake_whatsapp.sent[0].body

    async def test_a_sent_handoff_resolves_the_call(self, session, call):
        await whatsapp.initiate_handoff(session, call, reason=HandoffReason.LOW_CONFIDENCE)

        assert call.resolution == CallResolution.HANDED_OFF

    async def test_the_message_goes_to_the_caller_by_default(self, session, call, fake_whatsapp):
        handoff = await whatsapp.initiate_handoff(
            session, call, reason=HandoffReason.CALLER_REQUEST
        )

        assert handoff.to_number == call.caller_number
        assert fake_whatsapp.sent[0].to_number == call.caller_number

    async def test_an_explicit_number_overrides_the_caller(self, session, call, fake_whatsapp):
        """The caller may be on a landline and give a different mobile."""
        handoff = await whatsapp.initiate_handoff(
            session, call, reason=HandoffReason.CALLER_REQUEST, to_number="+919812300000"
        )

        assert handoff.to_number == "+919812300000"
        assert fake_whatsapp.sent[0].to_number == "+919812300000"

    async def test_the_provider_gets_the_call_and_reason_as_context(
        self, session, call, fake_whatsapp
    ):
        await whatsapp.initiate_handoff(session, call, reason=HandoffReason.DOCUMENT_REQUIRED)

        assert fake_whatsapp.sent[0].context == {
            "call_id": str(call.id),
            "reason": "document_required",
        }

    async def test_the_transcript_is_carried_into_the_context(self, session, call):
        await add_turns(
            session,
            call,
            (SpeakerRole.AGENT, "Namaste, kaise madad karun?"),
            (SpeakerRole.CALLER, "Mujhe blood test karana hai."),
        )

        handoff = await whatsapp.initiate_handoff(
            session, call, reason=HandoffReason.CALLER_REQUEST
        )

        transcript = handoff.context_json["transcript"]
        assert [turn["role"] for turn in transcript] == [SpeakerRole.AGENT, SpeakerRole.CALLER]
        assert transcript[1]["content"] == "Mujhe blood test karana hai."

    async def test_the_confidence_that_triggered_it_is_kept(self, session, call):
        handoff = await whatsapp.initiate_handoff(
            session, call, reason=HandoffReason.LOW_CONFIDENCE, confidence=0.31
        )

        assert handoff.confidence_at_handoff == pytest.approx(0.31)


class TestHandoffSummary:
    async def test_an_explicit_summary_wins(self, session, call, fake_whatsapp):
        call.summary = "Summary from the call record."

        await whatsapp.initiate_handoff(
            session, call, reason=HandoffReason.CALLER_REQUEST, summary="Explicit summary."
        )

        assert "Explicit summary." in fake_whatsapp.sent[0].body

    async def test_the_call_summary_is_used_when_none_is_given(self, session, call, fake_whatsapp):
        call.summary = "Summary from the call record."

        await whatsapp.initiate_handoff(session, call, reason=HandoffReason.CALLER_REQUEST)

        assert "Summary from the call record." in fake_whatsapp.sent[0].body

    async def test_falls_back_to_what_the_caller_asked_for(self, session, call, fake_whatsapp):
        call.summary = None
        await add_turns(
            session,
            call,
            (SpeakerRole.AGENT, "Namaste!"),
            (SpeakerRole.CALLER, "Mujhe report chahiye."),
            (SpeakerRole.CALLER, "Aur timing bhi."),
        )

        await whatsapp.initiate_handoff(session, call, reason=HandoffReason.CALLER_REQUEST)

        body = fake_whatsapp.sent[0].body
        assert "You asked about: Mujhe report chahiye.; Aur timing bhi." in body
        # The agent's own lines are not the caller's request.
        assert "Namaste!" not in body

    async def test_the_fallback_summarises_at_most_three_caller_turns(
        self, session, call, fake_whatsapp
    ):
        call.summary = None
        await add_turns(session, call, *[(SpeakerRole.CALLER, f"point {n}") for n in range(1, 6)])

        await whatsapp.initiate_handoff(session, call, reason=HandoffReason.CALLER_REQUEST)

        body = fake_whatsapp.sent[0].body
        assert "point 3" in body
        assert "point 4" not in body

    async def test_a_silent_caller_still_gets_a_usable_message(self, session, call):
        """No caller turns at all — the message must still say something."""
        call.summary = None
        await add_turns(session, call, (SpeakerRole.AGENT, "Namaste!"))

        handoff = await whatsapp.initiate_handoff(session, call, reason=HandoffReason.AGENT_ERROR)

        assert handoff.summary == "We were unable to complete your request over the phone."
        assert handoff.status == HandoffStatus.SENT


class TestHandoffLanguage:
    async def test_hindi_is_the_default_template(self, session, call, fake_whatsapp):
        await whatsapp.initiate_handoff(
            session, call, reason=HandoffReason.CALLER_REQUEST, summary="ok"
        )

        assert "नमस्ते" in fake_whatsapp.sent[0].body

    async def test_english_uses_the_english_template(self, session, call, fake_whatsapp):
        await whatsapp.initiate_handoff(
            session,
            call,
            reason=HandoffReason.CALLER_REQUEST,
            summary="ok",
            language=Language.ENGLISH,
        )

        assert fake_whatsapp.sent[0].body.startswith("Hello!")

    async def test_a_language_without_a_template_falls_back_to_english(
        self, session, call, fake_whatsapp
    ):
        """Every supported language must produce a message; a missing template
        must not send the literal ``{summary}`` placeholder or crash."""
        await whatsapp.initiate_handoff(
            session, call, reason=HandoffReason.CALLER_REQUEST, summary="ok", language="ta"
        )

        body = fake_whatsapp.sent[0].body
        assert body.startswith("Hello!")
        assert "{summary}" not in body

    async def test_the_language_is_recorded_for_the_retry(self, session, call):
        handoff = await whatsapp.initiate_handoff(
            session,
            call,
            reason=HandoffReason.CALLER_REQUEST,
            summary="ok",
            language=Language.ENGLISH,
        )

        assert handoff.context_json["language"] == "en"


class TestHandoffFailure:
    async def test_a_rejected_send_is_recorded_not_swallowed(self, session, call, fake_whatsapp):
        fake_whatsapp.fail = True

        handoff = await whatsapp.initiate_handoff(
            session, call, reason=HandoffReason.CALLER_REQUEST
        )

        assert handoff.status == HandoffStatus.FAILED
        assert handoff.error_message == "injected provider failure"
        assert handoff.provider_message_id is None
        assert handoff.sent_at is None

    async def test_a_failed_handoff_does_not_resolve_the_call(self, session, call, fake_whatsapp):
        """The caller was never messaged, so the call is still unresolved and
        must stay in whatever queue picks up unresolved calls."""
        fake_whatsapp.fail = True

        await whatsapp.initiate_handoff(session, call, reason=HandoffReason.CALLER_REQUEST)

        assert call.resolution != CallResolution.HANDED_OFF

    async def test_a_failed_handoff_is_still_persisted(self, session, call, fake_whatsapp):
        fake_whatsapp.fail = True

        await whatsapp.initiate_handoff(session, call, reason=HandoffReason.CALLER_REQUEST)

        assert len(await handoffs_for(session, call)) == 1

    async def test_a_long_provider_error_is_truncated_to_the_column(
        self, session, call, fake_whatsapp, monkeypatch
    ):
        """``error_message`` is a String(500); an overlong provider error must
        be trimmed here rather than blowing up the insert on Postgres."""

        async def _send(_message):
            return whatsapp.WhatsAppResult(message_id="", accepted=False, error="x" * 900)

        monkeypatch.setattr(fake_whatsapp, "send", _send)

        handoff = await whatsapp.initiate_handoff(
            session, call, reason=HandoffReason.CALLER_REQUEST
        )

        assert handoff.error_message == "x" * 500

    async def test_a_provider_without_an_error_message_still_records_one(
        self, session, call, fake_whatsapp, monkeypatch
    ):
        async def _send(_message):
            return whatsapp.WhatsAppResult(message_id="", accepted=False, error=None)

        monkeypatch.setattr(fake_whatsapp, "send", _send)

        handoff = await whatsapp.initiate_handoff(
            session, call, reason=HandoffReason.CALLER_REQUEST
        )

        assert handoff.status == HandoffStatus.FAILED
        assert handoff.error_message == "Unknown error"


class TestHandoffIdempotency:
    async def test_a_second_handoff_reuses_the_sent_one(self, session, call, fake_whatsapp):
        """Two turns can both trip the confidence threshold; the caller must
        not receive the same summary twice."""
        first = await whatsapp.initiate_handoff(session, call, reason=HandoffReason.LOW_CONFIDENCE)
        second = await whatsapp.initiate_handoff(session, call, reason=HandoffReason.CALLER_REQUEST)

        assert second.id == first.id
        assert len(fake_whatsapp.sent) == 1
        assert len(await handoffs_for(session, call)) == 1

    async def test_an_acknowledged_handoff_also_blocks_a_resend(self, session, call, fake_whatsapp):
        first = await whatsapp.initiate_handoff(session, call, reason=HandoffReason.CALLER_REQUEST)
        first.status = HandoffStatus.ACKNOWLEDGED
        await session.flush()

        second = await whatsapp.initiate_handoff(session, call, reason=HandoffReason.CALLER_REQUEST)

        assert second.id == first.id
        assert len(fake_whatsapp.sent) == 1

    async def test_a_failed_handoff_does_not_block_a_fresh_attempt(
        self, session, call, fake_whatsapp
    ):
        """Only a delivered message is a reason not to try again."""
        fake_whatsapp.fail = True
        failed = await whatsapp.initiate_handoff(session, call, reason=HandoffReason.CALLER_REQUEST)

        fake_whatsapp.fail = False
        retried = await whatsapp.initiate_handoff(
            session, call, reason=HandoffReason.CALLER_REQUEST
        )

        assert retried.id != failed.id
        assert retried.status == HandoffStatus.SENT
        # The fake records attempts, not deliveries: two were made, and only
        # the second one was accepted.
        assert len(fake_whatsapp.sent) == 2

    async def test_another_call_gets_its_own_handoff(
        self, session, business, agent, phone_number, call
    ):
        second_call = CallLog(
            business_id=business.id,
            agent_id=agent.id,
            phone_number_id=phone_number.id,
            direction=CallDirection.INBOUND,
            status=CallStatus.IN_PROGRESS,
            caller_number="+919999977777",
            callee_number=phone_number.number,
            provider=TelephonyProvider.MOCK,
            provider_call_id="mock-call-2",
            started_at=datetime.now(UTC),
            language=Language.HINDI,
        )
        session.add(second_call)
        await session.flush()

        first = await whatsapp.initiate_handoff(session, call, reason=HandoffReason.CALLER_REQUEST)
        second = await whatsapp.initiate_handoff(
            session, second_call, reason=HandoffReason.CALLER_REQUEST
        )

        assert first.id != second.id


# --------------------------------------------------------------------------- #
# Retry
# --------------------------------------------------------------------------- #
class TestRetryHandoff:
    async def test_a_failed_handoff_can_be_resent(self, session, business, call, fake_whatsapp):
        fake_whatsapp.fail = True
        handoff = await whatsapp.initiate_handoff(
            session, call, reason=HandoffReason.CALLER_REQUEST, summary="Report ready."
        )

        fake_whatsapp.fail = False
        retried = await whatsapp.retry_handoff(session, business.id, handoff.id)

        assert retried.id == handoff.id
        assert retried.status == HandoffStatus.SENT
        assert retried.provider_message_id == "fake-wa-1"
        assert retried.sent_at is not None

    async def test_a_successful_retry_clears_the_stale_error(
        self, session, business, call, fake_whatsapp
    ):
        """A row showing both ``sent`` and last run's error message would make
        the dashboard read as if the send had failed."""
        fake_whatsapp.fail = True
        handoff = await whatsapp.initiate_handoff(
            session, call, reason=HandoffReason.CALLER_REQUEST
        )
        assert handoff.error_message is not None

        fake_whatsapp.fail = False
        retried = await whatsapp.retry_handoff(session, business.id, handoff.id)

        assert retried.error_message is None

    async def test_the_retry_reuses_the_stored_summary(
        self, session, business, call, fake_whatsapp
    ):
        fake_whatsapp.fail = True
        handoff = await whatsapp.initiate_handoff(
            session, call, reason=HandoffReason.CALLER_REQUEST, summary="Report ready."
        )

        fake_whatsapp.fail = False
        await whatsapp.retry_handoff(session, business.id, handoff.id)

        assert "Report ready." in fake_whatsapp.sent[-1].body

    async def test_the_retry_keeps_the_original_language(
        self, session, business, call, fake_whatsapp
    ):
        fake_whatsapp.fail = True
        handoff = await whatsapp.initiate_handoff(
            session,
            call,
            reason=HandoffReason.CALLER_REQUEST,
            summary="ok",
            language=Language.ENGLISH,
        )

        fake_whatsapp.fail = False
        await whatsapp.retry_handoff(session, business.id, handoff.id)

        assert fake_whatsapp.sent[-1].body.startswith("Hello!")

    async def test_retrying_a_sent_handoff_does_nothing(
        self, session, business, call, fake_whatsapp
    ):
        """Retry must be safe to click twice."""
        handoff = await whatsapp.initiate_handoff(
            session, call, reason=HandoffReason.CALLER_REQUEST
        )

        retried = await whatsapp.retry_handoff(session, business.id, handoff.id)

        assert retried.status == HandoffStatus.SENT
        assert len(fake_whatsapp.sent) == 1

    async def test_a_retry_that_fails_again_keeps_the_new_error(
        self, session, business, call, fake_whatsapp
    ):
        fake_whatsapp.fail = True
        handoff = await whatsapp.initiate_handoff(
            session, call, reason=HandoffReason.CALLER_REQUEST
        )

        retried = await whatsapp.retry_handoff(session, business.id, handoff.id)

        assert retried.status == HandoffStatus.FAILED
        assert retried.error_message == "injected provider failure"

    async def test_an_unknown_handoff_is_not_found(self, session, business):
        with pytest.raises(NotFoundError):
            await whatsapp.retry_handoff(session, business.id, uuid.uuid4())

    async def test_another_tenants_handoff_is_not_found(
        self, session, other_business, call, fake_whatsapp
    ):
        fake_whatsapp.fail = True
        handoff = await whatsapp.initiate_handoff(
            session, call, reason=HandoffReason.CALLER_REQUEST
        )

        with pytest.raises(NotFoundError):
            await whatsapp.retry_handoff(session, other_business.id, handoff.id)


# --------------------------------------------------------------------------- #
# HTTP surface
# --------------------------------------------------------------------------- #
class TestHandoffEndpoint:
    async def test_an_operator_can_hand_a_call_off(
        self, client: AsyncClient, owner_headers, call, fake_whatsapp
    ):
        response = await client.post(
            f"/api/v1/whatsapp/handoff/{call.id}",
            headers=owner_headers,
            json={"reason": "caller_request", "summary": "Sending the report."},
        )

        assert response.status_code == 201
        body = response.json()
        assert body["call_id"] == str(call.id)
        assert body["status"] == HandoffStatus.SENT
        assert body["reason"] == HandoffReason.CALLER_REQUEST
        assert len(fake_whatsapp.sent) == 1

    async def test_a_supervisor_can_hand_a_call_off(
        self, client: AsyncClient, supervisor_headers, call
    ):
        response = await client.post(
            f"/api/v1/whatsapp/handoff/{call.id}",
            headers=supervisor_headers,
            json={"reason": "caller_request"},
        )

        assert response.status_code == 201

    async def test_a_viewer_cannot_hand_a_call_off(
        self, client: AsyncClient, viewer_headers, call, fake_whatsapp
    ):
        response = await client.post(
            f"/api/v1/whatsapp/handoff/{call.id}",
            headers=viewer_headers,
            json={"reason": "caller_request"},
        )

        assert response.status_code == 403
        assert fake_whatsapp.sent == []

    async def test_the_endpoint_requires_authentication(self, client: AsyncClient, call):
        response = await client.post(
            f"/api/v1/whatsapp/handoff/{call.id}", json={"reason": "caller_request"}
        )

        assert response.status_code == 401

    async def test_an_unknown_call_is_a_404(self, client: AsyncClient, owner_headers):
        response = await client.post(
            f"/api/v1/whatsapp/handoff/{uuid.uuid4()}",
            headers=owner_headers,
            json={"reason": "caller_request"},
        )

        assert response.status_code == 404

    async def test_another_tenants_call_is_a_404(
        self, client: AsyncClient, other_headers, call, fake_whatsapp
    ):
        """Not a 403: the other tenant must not learn the call exists."""
        response = await client.post(
            f"/api/v1/whatsapp/handoff/{call.id}",
            headers=other_headers,
            json={"reason": "caller_request"},
        )

        assert response.status_code == 404
        assert fake_whatsapp.sent == []

    async def test_the_target_number_is_normalised(self, client: AsyncClient, owner_headers, call):
        response = await client.post(
            f"/api/v1/whatsapp/handoff/{call.id}",
            headers=owner_headers,
            json={"reason": "caller_request", "to_number": "98123 00000"},
        )

        assert response.status_code == 201
        assert response.json()["to_number"] == "+919812300000"

    async def test_an_unknown_reason_is_rejected(self, client: AsyncClient, owner_headers, call):
        response = await client.post(
            f"/api/v1/whatsapp/handoff/{call.id}",
            headers=owner_headers,
            json={"reason": "because-i-said-so"},
        )

        assert response.status_code == 422

    async def test_a_repeated_request_returns_the_same_handoff(
        self, client: AsyncClient, owner_headers, call, fake_whatsapp
    ):
        first = await client.post(
            f"/api/v1/whatsapp/handoff/{call.id}",
            headers=owner_headers,
            json={"reason": "caller_request"},
        )
        second = await client.post(
            f"/api/v1/whatsapp/handoff/{call.id}",
            headers=owner_headers,
            json={"reason": "caller_request"},
        )

        assert second.status_code == 201
        assert second.json()["id"] == first.json()["id"]
        assert len(fake_whatsapp.sent) == 1


class TestListHandoffs:
    async def test_lists_the_tenants_handoffs(
        self, client: AsyncClient, owner_headers, session, call
    ):
        await whatsapp.initiate_handoff(session, call, reason=HandoffReason.CALLER_REQUEST)

        response = await client.get("/api/v1/whatsapp/handoffs", headers=owner_headers)

        assert response.status_code == 200
        body = response.json()
        assert body["total"] == 1
        assert body["items"][0]["call_id"] == str(call.id)

    async def test_a_viewer_may_read_handoffs(
        self, client: AsyncClient, viewer_headers, session, call
    ):
        await whatsapp.initiate_handoff(session, call, reason=HandoffReason.CALLER_REQUEST)

        response = await client.get("/api/v1/whatsapp/handoffs", headers=viewer_headers)

        assert response.status_code == 200
        assert response.json()["total"] == 1

    async def test_another_tenant_sees_nothing(
        self, client: AsyncClient, other_headers, session, call
    ):
        await whatsapp.initiate_handoff(session, call, reason=HandoffReason.CALLER_REQUEST)

        response = await client.get("/api/v1/whatsapp/handoffs", headers=other_headers)

        assert response.status_code == 200
        assert response.json() == {"items": [], "total": 0, "limit": 50, "offset": 0}

    async def test_filters_by_call(
        self, client: AsyncClient, owner_headers, session, business, agent, phone_number, call
    ):
        other_call = CallLog(
            business_id=business.id,
            agent_id=agent.id,
            phone_number_id=phone_number.id,
            direction=CallDirection.INBOUND,
            status=CallStatus.IN_PROGRESS,
            caller_number="+919999966666",
            callee_number=phone_number.number,
            provider=TelephonyProvider.MOCK,
            provider_call_id="mock-call-3",
            started_at=datetime.now(UTC),
            language=Language.HINDI,
        )
        session.add(other_call)
        await session.flush()
        await whatsapp.initiate_handoff(session, call, reason=HandoffReason.CALLER_REQUEST)
        await whatsapp.initiate_handoff(session, other_call, reason=HandoffReason.CALLER_REQUEST)

        response = await client.get(
            "/api/v1/whatsapp/handoffs", headers=owner_headers, params={"call_id": str(call.id)}
        )

        assert response.status_code == 200
        body = response.json()
        assert body["total"] == 1
        assert body["items"][0]["call_id"] == str(call.id)

    async def test_filters_by_status(
        self, client: AsyncClient, owner_headers, session, call, fake_whatsapp
    ):
        fake_whatsapp.fail = True
        await whatsapp.initiate_handoff(session, call, reason=HandoffReason.CALLER_REQUEST)

        failed = await client.get(
            "/api/v1/whatsapp/handoffs", headers=owner_headers, params={"status": "failed"}
        )
        sent = await client.get(
            "/api/v1/whatsapp/handoffs", headers=owner_headers, params={"status": "sent"}
        )

        assert failed.json()["total"] == 1
        assert sent.json()["total"] == 0

    async def test_an_invalid_status_filter_is_a_422(self, client: AsyncClient, owner_headers):
        response = await client.get(
            "/api/v1/whatsapp/handoffs", headers=owner_headers, params={"status": "nope"}
        )

        assert response.status_code == 422

    async def test_the_total_counts_beyond_the_page(
        self, client: AsyncClient, owner_headers, session, business, agent, phone_number
    ):
        """``total`` drives the pager, so it must count the whole result set
        rather than the rows on this page."""
        for index in range(3):
            row = CallLog(
                business_id=business.id,
                agent_id=agent.id,
                phone_number_id=phone_number.id,
                direction=CallDirection.INBOUND,
                status=CallStatus.IN_PROGRESS,
                caller_number=f"+91999990000{index}",
                callee_number=phone_number.number,
                provider=TelephonyProvider.MOCK,
                provider_call_id=f"mock-page-{index}",
                started_at=datetime.now(UTC),
                language=Language.HINDI,
            )
            session.add(row)
            await session.flush()
            await whatsapp.initiate_handoff(session, row, reason=HandoffReason.CALLER_REQUEST)

        response = await client.get(
            "/api/v1/whatsapp/handoffs", headers=owner_headers, params={"limit": 2}
        )

        body = response.json()
        assert body["total"] == 3
        assert len(body["items"]) == 2
        assert body["limit"] == 2


class TestRetryEndpoint:
    async def test_an_operator_can_retry_a_failed_handoff(
        self, client: AsyncClient, owner_headers, session, call, fake_whatsapp
    ):
        fake_whatsapp.fail = True
        handoff = await whatsapp.initiate_handoff(
            session, call, reason=HandoffReason.CALLER_REQUEST
        )
        fake_whatsapp.fail = False

        response = await client.post(
            f"/api/v1/whatsapp/handoffs/{handoff.id}/retry", headers=owner_headers
        )

        assert response.status_code == 200
        assert response.json()["status"] == HandoffStatus.SENT

    async def test_a_viewer_cannot_retry(
        self, client: AsyncClient, viewer_headers, session, call, fake_whatsapp
    ):
        fake_whatsapp.fail = True
        handoff = await whatsapp.initiate_handoff(
            session, call, reason=HandoffReason.CALLER_REQUEST
        )

        response = await client.post(
            f"/api/v1/whatsapp/handoffs/{handoff.id}/retry", headers=viewer_headers
        )

        assert response.status_code == 403

    async def test_an_unknown_handoff_is_a_404(self, client: AsyncClient, owner_headers):
        response = await client.post(
            f"/api/v1/whatsapp/handoffs/{uuid.uuid4()}/retry", headers=owner_headers
        )

        assert response.status_code == 404

    async def test_another_tenant_cannot_retry(
        self, client: AsyncClient, other_headers, session, call, fake_whatsapp
    ):
        fake_whatsapp.fail = True
        handoff = await whatsapp.initiate_handoff(
            session, call, reason=HandoffReason.CALLER_REQUEST
        )

        response = await client.post(
            f"/api/v1/whatsapp/handoffs/{handoff.id}/retry", headers=other_headers
        )

        assert response.status_code == 404
