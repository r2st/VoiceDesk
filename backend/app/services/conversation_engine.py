"""The AI conversation engine.

Drives one call turn at a time: takes the caller's transcribed utterance,
advances the agent's conversation flow, asks the LLM for a reply when the flow
hands control over, and persists both sides of the exchange.

The engine is transport-agnostic — it never touches audio. The ASR/TTS edges
feed it text and speak its replies, so the whole dialogue layer is testable
without a telephony network.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models.call import CallLog, Conversation
from app.models.enums import (
    CallResolution,
    HandoffReason,
    Language,
    Sentiment,
    SpeakerRole,
)
from app.models.voice_agent import VoiceAgent
from app.services import compliance, nlu
from app.services.flow import ConversationFlow, NodeType, validate_flow
from app.services.llm import LLMMessage, OpenRouterClient, get_llm_client

logger = get_logger(__name__)

#: How many prior turns are replayed to the LLM as context.
CONTEXT_WINDOW_TURNS = 12

#: Spoken replies must stay short — this is a phone call, not a chat window.
MAX_REPLY_TOKENS = 160


@dataclass(slots=True)
class TurnResult:
    """What the engine decided for one caller utterance."""

    reply: str
    language: Language
    confidence: float
    node_id: str | None = None
    detected_intent: str | None = None
    sentiment: Sentiment = Sentiment.NEUTRAL
    sentiment_score: float = 0.0
    should_end_call: bool = False
    should_handoff: bool = False
    handoff_reason: HandoffReason | None = None
    transfer_to: str | None = None
    disposition: CallResolution | None = None
    latency_ms: int = 0
    model_used: str | None = None
    variables: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class CallState:
    """Mutable per-call state, persisted on ``CallLog.metadata_json``."""

    current_node: str | None = None
    variables: dict[str, Any] = field(default_factory=dict)
    retries: dict[str, int] = field(default_factory=dict)
    turn_index: int = 0
    consent_announced: bool = False

    @classmethod
    def from_call(cls, call: CallLog) -> CallState:
        raw = (call.metadata_json or {}).get("state") or {}
        return cls(
            current_node=raw.get("current_node"),
            variables=dict(raw.get("variables") or {}),
            retries=dict(raw.get("retries") or {}),
            turn_index=int(raw.get("turn_index") or 0),
            consent_announced=bool(raw.get("consent_announced") or False),
        )

    def save_to(self, call: CallLog) -> None:
        metadata = dict(call.metadata_json or {})
        metadata["state"] = {
            "current_node": self.current_node,
            "variables": self.variables,
            "retries": self.retries,
            "turn_index": self.turn_index,
            "consent_announced": self.consent_announced,
        }
        call.metadata_json = metadata


class ConversationEngine:
    def __init__(self, llm: OpenRouterClient | None = None) -> None:
        self._llm = llm

    @property
    def llm(self) -> OpenRouterClient:
        return self._llm or get_llm_client()

    # ------------------------------------------------------------------ #
    # Call opening
    # ------------------------------------------------------------------ #
    async def start_call(
        self, session: AsyncSession, call: CallLog, agent: VoiceAgent
    ) -> TurnResult:
        """Produce the opening utterance: consent announcement plus greeting."""
        state = CallState.from_call(call)
        flow = validate_flow(agent.flow_json)
        language = Language(agent.language)

        parts: list[str] = []
        announcement = compliance.consent_announcement(language.value)
        if announcement and agent.recording_enabled and not state.consent_announced:
            parts.append(announcement)
            state.consent_announced = True
            call.consent_announced = True

        greeting = agent.greeting or "Hello! How may I help you today?"
        node_id: str | None = None
        if flow is not None:
            node = flow.get_node(flow.start_node)
            node_id = flow.start_node
            if node is not None and node.type is NodeType.MESSAGE:
                greeting = node.text
                state.current_node = node.next or flow.start_node
            else:
                state.current_node = flow.start_node
        parts.append(greeting)

        reply = " ".join(part.strip() for part in parts if part and part.strip())
        await self._persist_turn(
            session,
            call,
            state,
            role=SpeakerRole.AGENT,
            content=reply,
            language=language,
            confidence=1.0,
            node_id=node_id,
        )
        state.save_to(call)
        await session.flush()

        return TurnResult(
            reply=reply, language=language, confidence=1.0, node_id=state.current_node
        )

    # ------------------------------------------------------------------ #
    # Per-turn processing
    # ------------------------------------------------------------------ #
    async def process_turn(
        self,
        session: AsyncSession,
        call: CallLog,
        agent: VoiceAgent,
        utterance: str,
        *,
        asr_confidence: float = 1.0,
    ) -> TurnResult:
        """Handle one caller utterance and return the agent's response."""
        started = datetime.now(UTC)
        state = CallState.from_call(call)
        flow = validate_flow(agent.flow_json)

        allowed = _allowed_languages(agent)
        language_guess = await nlu.detect_language(utterance, allowed=allowed, client=self._llm)
        sentiment_guess = await nlu.analyze_sentiment(utterance, client=self._llm)

        await self._persist_turn(
            session,
            call,
            state,
            role=SpeakerRole.CALLER,
            content=utterance,
            language=language_guess.language,
            confidence=asr_confidence,
            sentiment=sentiment_guess.sentiment,
            node_id=state.current_node,
        )

        # TRAI §8.2.5 — an opt-out request ends the call immediately and adds
        # the caller to this tenant's suppression list.
        if compliance.detect_opt_out(utterance):
            await compliance.record_opt_out(session, call.caller_number, call.business_id)
            call.opted_out = True
            reply = _localised(
                language_guess.language,
                "आपका नंबर हमारी कॉल सूची से हटा दिया गया है। धन्यवाद।",
                "Your number has been removed from our calling list. Thank you.",
            )
            return await self._finalise_turn(
                session,
                call,
                state,
                TurnResult(
                    reply=reply,
                    language=language_guess.language,
                    confidence=1.0,
                    should_end_call=True,
                    disposition=CallResolution.RESOLVED,
                    sentiment=sentiment_guess.sentiment,
                    sentiment_score=sentiment_guess.score,
                    latency_ms=_elapsed_ms(started),
                ),
            )

        intents = await self._agent_intents(session, call.business_id, agent.id)
        intent_guess = await nlu.classify_intent(utterance, intents, client=self._llm)

        result = (
            await self._advance_flow(
                session, call, agent, flow, state, utterance, language_guess, intent_guess
            )
            if flow is not None
            else await self._llm_reply(session, call, agent, state, utterance, language_guess)
        )

        result.sentiment = sentiment_guess.sentiment
        result.sentiment_score = sentiment_guess.score
        result.detected_intent = result.detected_intent or intent_guess.intent
        result.latency_ms = _elapsed_ms(started)

        # Confidence-triggered WhatsApp handoff (design doc §4.5).
        threshold = agent.handoff_confidence_threshold or 0.70
        if (
            agent.whatsapp_handoff_enabled
            and not result.should_handoff
            and result.confidence < threshold
        ):
            result.should_handoff = True
            result.handoff_reason = HandoffReason.LOW_CONFIDENCE
            result.reply = _localised(
                language_guess.language,
                "मैं इसे ठीक से समझ नहीं पाया। मैं आपको WhatsApp पर विवरण भेज रहा हूँ।",
                "I could not quite follow that. I am sending you the details on WhatsApp.",
            )

        return await self._finalise_turn(session, call, state, result)

    # ------------------------------------------------------------------ #
    # Flow traversal
    # ------------------------------------------------------------------ #
    async def _advance_flow(
        self,
        session: AsyncSession,
        call: CallLog,
        agent: VoiceAgent,
        flow: ConversationFlow,
        state: CallState,
        utterance: str,
        language_guess: nlu.LanguageGuess,
        intent_guess: nlu.IntentGuess,
    ) -> TurnResult:
        """Walk the flow graph until it produces something to say."""
        node_id = state.current_node or flow.start_node
        spoken: list[str] = []
        visited: set[str] = set()

        for _ in range(len(flow.nodes) + 1):
            node = flow.get_node(node_id) if node_id else None
            if node is None:
                break
            if node.id in visited:
                logger.warning("Flow loop detected at node %s; breaking", node.id)
                break
            visited.add(node.id)

            if node.type is NodeType.MESSAGE:
                spoken.append(node.text)
                node_id = node.next
                if node_id is None:
                    break
                continue

            if node.type is NodeType.COLLECT:
                # The caller's utterance answers this node's prompt.
                state.variables[node.variable] = utterance
                node_id = node.next
                if node_id is None:
                    break
                continue

            if node.type is NodeType.INTENT_BRANCH:
                target = (
                    node.branches.get(intent_guess.intent) if intent_guess.intent else None
                ) or node.default
                if target is None:
                    break
                node_id = target
                continue

            if node.type is NodeType.CONDITION:
                matched = _evaluate_condition(node, state.variables)
                node_id = node.if_true if matched else node.if_false
                if node_id is None:
                    break
                continue

            if node.type is NodeType.API_CALL:
                ok = await self._call_api_node(node, state)
                node_id = node.next if ok else (node.on_error or node.next)
                if node_id is None:
                    break
                continue

            if node.type is NodeType.HANDOFF:
                message = node.message or _localised(
                    language_guess.language,
                    "मैं आपको WhatsApp पर विवरण भेज रहा हूँ।",
                    "I am sending you the details on WhatsApp.",
                )
                spoken.append(message)
                state.current_node = node.next
                return TurnResult(
                    reply=" ".join(spoken),
                    language=language_guess.language,
                    confidence=max(intent_guess.confidence, 0.8),
                    node_id=node.id,
                    detected_intent=intent_guess.intent,
                    should_handoff=True,
                    handoff_reason=HandoffReason.UNSUPPORTED_INTENT,
                    should_end_call=node.next is None,
                    disposition=CallResolution.HANDED_OFF,
                    variables=dict(state.variables),
                )

            if node.type is NodeType.TRANSFER:
                if node.announcement:
                    spoken.append(node.announcement)
                state.current_node = None
                return TurnResult(
                    reply=" ".join(spoken) or "Connecting you to a colleague now.",
                    language=language_guess.language,
                    confidence=1.0,
                    node_id=node.id,
                    detected_intent=intent_guess.intent,
                    transfer_to=node.to_number,
                    should_end_call=True,
                    disposition=CallResolution.ESCALATED,
                    variables=dict(state.variables),
                )

            if node.type is NodeType.END:
                if node.text:
                    spoken.append(node.text)
                state.current_node = None
                return TurnResult(
                    reply=" ".join(spoken) or "Thank you for calling.",
                    language=language_guess.language,
                    confidence=1.0,
                    node_id=node.id,
                    detected_intent=intent_guess.intent,
                    should_end_call=True,
                    disposition=CallResolution(node.disposition),
                    variables=dict(state.variables),
                )

            break

        state.current_node = node_id

        # The flow reached a node that asks a question (or ran dry): let the LLM
        # voice it in the caller's language with full conversation context.
        pending = flow.get_node(node_id) if node_id else None
        if pending is not None and pending.type is NodeType.COLLECT:
            spoken.append(pending.prompt)

        if spoken:
            return TurnResult(
                reply=" ".join(part for part in spoken if part),
                language=language_guess.language,
                confidence=max(intent_guess.confidence, 0.75),
                node_id=node_id,
                detected_intent=intent_guess.intent,
                variables=dict(state.variables),
            )

        return await self._llm_reply(session, call, agent, state, utterance, language_guess)

    async def _call_api_node(self, node, state: CallState) -> bool:
        """Execute an ``api_call`` node against a business system."""
        try:
            body = _render_template(node.body_template, state.variables)
            async with httpx.AsyncClient(timeout=node.timeout_seconds) as client:
                response = await client.request(
                    node.method,
                    _render_string(node.url, state.variables),
                    json=body if node.method != "GET" else None,
                    params=body if node.method == "GET" else None,
                    headers=node.headers or None,
                )
            if response.status_code >= 400:
                logger.warning("api_call node %s returned HTTP %s", node.id, response.status_code)
                return False
            payload = response.json() if response.content else {}
        except Exception as exc:
            logger.warning("api_call node %s failed: %s", node.id, exc)
            return False

        for variable, path in (node.save_as or {}).items():
            state.variables[variable] = _json_path(payload, path)
        return True

    # ------------------------------------------------------------------ #
    # LLM-driven reply
    # ------------------------------------------------------------------ #
    async def _llm_reply(
        self,
        session: AsyncSession,
        call: CallLog,
        agent: VoiceAgent,
        state: CallState,
        utterance: str,
        language_guess: nlu.LanguageGuess,
    ) -> TurnResult:
        history = await self._recent_turns(session, call)
        messages = [
            LLMMessage(role="system", content=_system_prompt(agent, language_guess.language, state))
        ]
        messages.extend(
            LLMMessage(
                role="assistant" if turn.role == SpeakerRole.AGENT else "user",
                content=turn.content,
            )
            for turn in history
        )
        if not history or history[-1].content != utterance:
            messages.append(LLMMessage(role="user", content=utterance))

        try:
            response = await self.llm.chat(messages, temperature=0.5, max_tokens=MAX_REPLY_TOKENS)
        except Exception as exc:
            logger.warning("LLM reply failed for call %s: %s", call.id, exc)
            fallback = agent.fallback_message or _localised(
                language_guess.language,
                "क्षमा करें, मुझे समझने में कठिनाई हो रही है। क्या आप दोहरा सकते हैं?",
                "Sorry, I am having trouble understanding. Could you repeat that?",
            )
            return TurnResult(
                reply=fallback,
                language=language_guess.language,
                confidence=0.3,
                node_id=state.current_node,
            )

        return TurnResult(
            reply=response.content,
            language=language_guess.language,
            confidence=min(language_guess.confidence + 0.1, 1.0),
            node_id=state.current_node,
            model_used=response.model,
            variables=dict(state.variables),
        )

    # ------------------------------------------------------------------ #
    # Persistence helpers
    # ------------------------------------------------------------------ #
    async def _finalise_turn(
        self, session: AsyncSession, call: CallLog, state: CallState, result: TurnResult
    ) -> TurnResult:
        await self._persist_turn(
            session,
            call,
            state,
            role=SpeakerRole.AGENT,
            content=result.reply,
            language=result.language,
            confidence=result.confidence,
            node_id=result.node_id,
            intent=result.detected_intent,
            latency_ms=result.latency_ms,
            model_used=result.model_used,
        )

        call.language = result.language.value
        detected = list(call.detected_languages or [])
        if result.language.value not in detected:
            detected.append(result.language.value)
            call.detected_languages = detected
        if result.detected_intent and not call.primary_intent:
            call.primary_intent = result.detected_intent
        if result.disposition is not None:
            call.resolution = result.disposition

        state.save_to(call)
        await session.flush()
        return result

    async def _persist_turn(
        self,
        session: AsyncSession,
        call: CallLog,
        state: CallState,
        *,
        role: SpeakerRole,
        content: str,
        language: Language,
        confidence: float | None,
        node_id: str | None = None,
        intent: str | None = None,
        sentiment: Sentiment | None = None,
        latency_ms: int | None = None,
        model_used: str | None = None,
    ) -> Conversation:
        turn = Conversation(
            business_id=call.business_id,
            call_id=call.id,
            turn_index=state.turn_index,
            role=role,
            content=content,
            language=language.value,
            confidence=confidence,
            sentiment=sentiment.value if sentiment else None,
            detected_intent=intent,
            flow_node_id=node_id,
            latency_ms=latency_ms,
            model_used=model_used,
        )
        session.add(turn)
        state.turn_index += 1
        await session.flush()
        return turn

    async def _recent_turns(
        self, session: AsyncSession, call: CallLog, limit: int = CONTEXT_WINDOW_TURNS
    ) -> list[Conversation]:
        result = await session.execute(
            select(Conversation)
            .where(
                Conversation.call_id == call.id,
                Conversation.business_id == call.business_id,
                Conversation.deleted_at.is_(None),
            )
            .order_by(Conversation.turn_index.desc())
            .limit(limit)
        )
        return list(reversed(result.scalars().all()))

    async def _agent_intents(
        self, session: AsyncSession, business_id: uuid.UUID, agent_id: uuid.UUID | None
    ) -> list[dict]:
        from app.models.voice_agent import Intent

        stmt = select(Intent).where(
            Intent.business_id == business_id,
            Intent.deleted_at.is_(None),
            Intent.is_active.is_(True),
        )
        if agent_id is not None:
            stmt = stmt.where((Intent.agent_id == agent_id) | (Intent.agent_id.is_(None)))
        result = await session.execute(stmt.order_by(Intent.priority))
        return [
            {
                "name": intent.name,
                "description": intent.description,
                "sample_phrases": intent.sample_phrases or [],
                "action_type": intent.action_type,
            }
            for intent in result.scalars().all()
        ]

    # ------------------------------------------------------------------ #
    # Post-call
    # ------------------------------------------------------------------ #
    async def summarise_call(
        self, session: AsyncSession, call: CallLog
    ) -> tuple[str, Sentiment, float]:
        """Generate a post-call summary and roll up sentiment across turns."""
        turns = await self._recent_turns(session, call, limit=200)
        if not turns:
            return "", Sentiment.NEUTRAL, 0.0

        scores = [
            _sentiment_to_score(t.sentiment)
            for t in turns
            if t.role == SpeakerRole.CALLER and t.sentiment
        ]
        average = sum(scores) / len(scores) if scores else 0.0
        overall = (
            Sentiment.POSITIVE
            if average > 0.2
            else Sentiment.NEGATIVE
            if average < -0.2
            else Sentiment.NEUTRAL
        )

        transcript = "\n".join(f"{t.role}: {t.content}" for t in turns)[:6000]
        try:
            response = await self.llm.chat(
                [
                    LLMMessage(
                        role="system",
                        content=(
                            "Summarise this customer phone call for a business owner in "
                            "two or three sentences. State what the caller wanted and "
                            "whether it was resolved. Write in English."
                        ),
                    ),
                    LLMMessage(role="user", content=transcript),
                ],
                temperature=0.2,
                max_tokens=200,
            )
            summary = response.content
        except Exception as exc:
            logger.warning("Call summary failed for %s: %s", call.id, exc)
            summary = f"Call with {len(turns)} turns. Summary unavailable."

        confidences = [t.confidence for t in turns if t.confidence is not None]
        call.summary = summary
        call.sentiment = overall.value
        call.sentiment_score = round(average, 3)
        call.avg_confidence = round(sum(confidences) / len(confidences), 3) if confidences else None
        await session.flush()
        return summary, overall, average

    async def sentiment_trajectory(self, session: AsyncSession, call: CallLog) -> dict:
        turns = await self._recent_turns(session, call, limit=200)
        scores = [
            _sentiment_to_score(t.sentiment)
            for t in turns
            if t.role == SpeakerRole.CALLER and t.sentiment
        ]
        return nlu.sentiment_trajectory(scores)


# --------------------------------------------------------------------------- #
# Module helpers
# --------------------------------------------------------------------------- #
def _allowed_languages(agent: VoiceAgent) -> list[Language]:
    allowed = [Language(agent.language)]
    for code in agent.supported_languages or []:
        try:
            language = Language(code)
        except ValueError:
            continue
        if language not in allowed:
            allowed.append(language)
    return allowed


def _system_prompt(agent: VoiceAgent, language: Language, state: CallState) -> str:
    known = ", ".join(f"{k}={v}" for k, v in state.variables.items() if v) or "none yet"
    return (
        f"{agent.persona}\n\n"
        f"Respond in the caller's language ({language.value}). Keep the reply to one or "
        "two short sentences — it will be spoken aloud on a phone line, so avoid lists, "
        "markdown and long numbers. Information gathered so far: "
        f"{known}."
    )


def _localised(language: Language, hindi: str, english: str) -> str:
    return hindi if language is Language.HINDI else english


def _elapsed_ms(started: datetime) -> int:
    return int((datetime.now(UTC) - started).total_seconds() * 1000)


def _sentiment_to_score(sentiment: str | None) -> float:
    return {"positive": 1.0, "negative": -1.0}.get(sentiment or "", 0.0)


def _evaluate_condition(node, variables: dict) -> bool:
    actual = variables.get(node.variable)
    expected = node.value
    operator = node.operator

    if operator == "exists":
        return actual is not None
    if actual is None:
        return False
    if operator == "eq":
        return str(actual) == str(expected)
    if operator == "neq":
        return str(actual) != str(expected)
    if operator == "contains":
        return str(expected).lower() in str(actual).lower()
    try:
        left, right = float(actual), float(expected)
    except (TypeError, ValueError):
        return False
    return {
        "gt": left > right,
        "gte": left >= right,
        "lt": left < right,
        "lte": left <= right,
    }.get(operator, False)


def _render_string(template: str, variables: dict) -> str:
    """Substitute ``{{var}}`` placeholders."""
    rendered = template
    for key, value in variables.items():
        rendered = rendered.replace(f"{{{{{key}}}}}", str(value))
    return rendered


def _render_template(template: dict, variables: dict) -> dict:
    return {
        key: _render_string(value, variables) if isinstance(value, str) else value
        for key, value in (template or {}).items()
    }


def _json_path(payload: Any, path: str) -> Any:
    """Resolve a dotted/``$.``-prefixed path against a JSON payload."""
    current = payload
    for part in path.removeprefix("$.").split("."):
        if not part:
            continue
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list) and part.isdigit():
            index = int(part)
            current = current[index] if index < len(current) else None
        else:
            return None
    return current


_engine: ConversationEngine | None = None


def get_engine() -> ConversationEngine:
    global _engine
    if _engine is None:
        _engine = ConversationEngine()
    return _engine


def set_engine(engine: ConversationEngine | None) -> None:
    global _engine
    _engine = engine
