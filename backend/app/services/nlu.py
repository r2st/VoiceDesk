"""Language detection, intent classification and sentiment analysis.

Each function tries a cheap deterministic heuristic first and only escalates to
the LLM when the heuristic is not confident. On a phone call, a script-based
language guess is both faster and more reliable than a model round-trip, and it
keeps the latency budget (design doc §2.3) intact.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.core.logging import get_logger
from app.models.enums import Language, Sentiment
from app.services.llm import LLMMessage, OpenRouterClient, get_llm_client

logger = get_logger(__name__)

#: Unicode block per supported Indic script.
SCRIPT_RANGES: dict[Language, tuple[int, int]] = {
    Language.HINDI: (0x0900, 0x097F),  # Devanagari — also Marathi
    Language.BENGALI: (0x0980, 0x09FF),
    Language.TAMIL: (0x0B80, 0x0BFF),
    Language.TELUGU: (0x0C00, 0x0C7F),
    Language.KANNADA: (0x0C80, 0x0CFF),
}

#: Words that distinguish Marathi from Hindi — both use Devanagari.
MARATHI_MARKERS = ("आहे", "नाही", "काय", "तुम्ही", "माझ्या", "करू", "मला", "कसे")

#: Romanised Hindi/Hinglish, very common on Indian phone calls.
HINGLISH_MARKERS = (
    "kya",
    "hai",
    "nahi",
    "haan",
    "aap",
    "mera",
    "meri",
    "karo",
    "chahiye",
    "kitna",
    "kaise",
    "bhai",
    "theek",
    "acha",
    "batao",
    "kab",
)

POSITIVE_MARKERS = (
    "thank",
    "thanks",
    "great",
    "good",
    "perfect",
    "excellent",
    "happy",
    "dhanyavaad",
    "shukriya",
    "badhiya",
    "accha",
    "theek hai",
    "बढ़िया",
    "धन्यवाद",
)
NEGATIVE_MARKERS = (
    "angry",
    "terrible",
    "worst",
    "useless",
    "not working",
    "complaint",
    "refund",
    "cheated",
    "frustrated",
    "disappointed",
    "bakwas",
    "ganda",
    "गलत",
    "बकवास",
    "शिकायत",
    "पैसे वापस",
)


@dataclass(frozen=True, slots=True)
class LanguageGuess:
    language: Language
    confidence: float
    method: str


@dataclass(frozen=True, slots=True)
class IntentGuess:
    intent: str | None
    confidence: float
    method: str
    parameters: dict | None = None


@dataclass(frozen=True, slots=True)
class SentimentGuess:
    sentiment: Sentiment
    score: float
    method: str


def _script_of(text: str) -> Language | None:
    """The Indic script with the most characters in ``text``, if any."""
    counts: dict[Language, int] = {}
    for char in text:
        code = ord(char)
        for language, (low, high) in SCRIPT_RANGES.items():
            if low <= code <= high:
                counts[language] = counts.get(language, 0) + 1
                break
    if not counts:
        return None
    return max(counts, key=lambda k: counts[k])


def detect_language_heuristic(text: str) -> LanguageGuess:
    """Script- and keyword-based detection. No network call."""
    if not text or not text.strip():
        return LanguageGuess(Language.HINDI, 0.0, "empty")

    script = _script_of(text)
    if script is Language.HINDI:
        # Devanagari is shared; Marathi markers disambiguate.
        if any(marker in text for marker in MARATHI_MARKERS):
            return LanguageGuess(Language.MARATHI, 0.85, "script+markers")
        return LanguageGuess(Language.HINDI, 0.90, "script")
    if script is not None:
        return LanguageGuess(script, 0.95, "script")

    lowered = text.lower()
    words = re.findall(r"[a-z']+", lowered)
    if words:
        hits = sum(1 for word in words if word in HINGLISH_MARKERS)
        ratio = hits / len(words)
        if ratio >= 0.25:
            return LanguageGuess(Language.HINDI, min(0.85, 0.55 + ratio), "hinglish")
        if hits:
            return LanguageGuess(Language.HINDI, 0.55, "hinglish-weak")
        return LanguageGuess(Language.ENGLISH, 0.80, "latin-script")

    return LanguageGuess(Language.ENGLISH, 0.30, "fallback")


async def detect_language(
    text: str,
    *,
    allowed: list[Language] | None = None,
    client: OpenRouterClient | None = None,
    min_confidence: float = 0.70,
) -> LanguageGuess:
    """Heuristic first; escalate to the LLM only when the guess is weak."""
    guess = detect_language_heuristic(text)
    if guess.confidence >= min_confidence:
        return _constrain(guess, allowed)

    options = ", ".join(lang.value for lang in (allowed or list(Language)))
    try:
        llm = client or get_llm_client()
        data, _ = await llm.chat_json(
            [
                LLMMessage(
                    role="system",
                    content=(
                        "You identify the language of a phone-call utterance. "
                        f'Answer with JSON: {{"language": one of [{options}], '
                        '"confidence": 0.0-1.0}. Romanised Hindi (Hinglish) is "hi".'
                    ),
                ),
                LLMMessage(role="user", content=text[:500]),
            ],
            max_tokens=60,
        )
        language = Language(str(data.get("language", guess.language.value)))
        confidence = float(data.get("confidence", 0.6))
        return _constrain(LanguageGuess(language, confidence, "llm"), allowed)
    except Exception as exc:
        logger.warning("LLM language detection failed, using heuristic: %s", exc)
        return _constrain(guess, allowed)


def _constrain(guess: LanguageGuess, allowed: list[Language] | None) -> LanguageGuess:
    """Snap a detected language onto the set the agent actually supports."""
    if not allowed or guess.language in allowed:
        return guess
    return LanguageGuess(allowed[0], guess.confidence * 0.7, f"{guess.method}+constrained")


async def classify_intent(
    utterance: str,
    intents: list[dict],
    *,
    client: OpenRouterClient | None = None,
) -> IntentGuess:
    """Match an utterance to one of the business's configured intents.

    ``intents`` items look like
    ``{"name": ..., "description": ..., "sample_phrases": [...]}``.
    """
    if not intents or not utterance.strip():
        return IntentGuess(None, 0.0, "no-intents")

    # An exact/substring hit on a sample phrase is worth more than a model call.
    lowered = utterance.lower()
    for intent in intents:
        for phrase in intent.get("sample_phrases") or []:
            if phrase and phrase.lower() in lowered:
                return IntentGuess(intent["name"], 0.92, "phrase-match")

    catalog = "\n".join(
        f"- {i['name']}: {i.get('description', '')}"
        + (f" (e.g. {'; '.join(i['sample_phrases'][:3])})" if i.get("sample_phrases") else "")
        for i in intents
    )
    names = [i["name"] for i in intents]

    try:
        llm = client or get_llm_client()
        data, _ = await llm.chat_json(
            [
                LLMMessage(
                    role="system",
                    content=(
                        "Classify the caller's intent. Available intents:\n"
                        f"{catalog}\n\n"
                        'Reply with JSON: {"intent": <one of the names above, or null '
                        'if none fit>, "confidence": 0.0-1.0, "parameters": {}}. '
                        "Do not invent an intent name."
                    ),
                ),
                LLMMessage(role="user", content=utterance[:1000]),
            ],
            max_tokens=200,
        )
    except Exception as exc:
        logger.warning("Intent classification failed: %s", exc)
        return IntentGuess(None, 0.0, "error")

    name = data.get("intent")
    if name not in names:
        return IntentGuess(None, float(data.get("confidence", 0.0)), "llm-no-match")
    parameters = data.get("parameters")
    return IntentGuess(
        name,
        max(0.0, min(1.0, float(data.get("confidence", 0.5)))),
        "llm",
        parameters if isinstance(parameters, dict) else None,
    )


def analyze_sentiment_heuristic(text: str) -> SentimentGuess:
    lowered = (text or "").lower()
    positive = sum(1 for marker in POSITIVE_MARKERS if marker in lowered)
    negative = sum(1 for marker in NEGATIVE_MARKERS if marker in lowered)
    if positive == negative:
        return SentimentGuess(Sentiment.NEUTRAL, 0.0, "keywords")
    if positive > negative:
        return SentimentGuess(Sentiment.POSITIVE, min(1.0, 0.3 + 0.2 * positive), "keywords")
    return SentimentGuess(Sentiment.NEGATIVE, -min(1.0, 0.3 + 0.2 * negative), "keywords")


async def analyze_sentiment(
    text: str, *, client: OpenRouterClient | None = None, use_llm: bool = True
) -> SentimentGuess:
    """Sentiment on ``[-1, 1]``. Negative is unhappy."""
    heuristic = analyze_sentiment_heuristic(text)
    if not use_llm or heuristic.sentiment is not Sentiment.NEUTRAL:
        return heuristic
    if not text.strip():
        return heuristic

    try:
        llm = client or get_llm_client()
        data, _ = await llm.chat_json(
            [
                LLMMessage(
                    role="system",
                    content=(
                        "Rate the caller's sentiment in this phone-call utterance. "
                        'Reply with JSON: {"sentiment": "positive"|"neutral"|"negative", '
                        '"score": -1.0 to 1.0}.'
                    ),
                ),
                LLMMessage(role="user", content=text[:1000]),
            ],
            max_tokens=60,
        )
        sentiment = Sentiment(str(data.get("sentiment", "neutral")))
        score = max(-1.0, min(1.0, float(data.get("score", 0.0))))
        return SentimentGuess(sentiment, score, "llm")
    except Exception as exc:
        logger.warning("Sentiment analysis failed: %s", exc)
        return heuristic


def sentiment_trajectory(scores: list[float]) -> dict:
    """Summarise how sentiment moved across a call (design doc §4.4)."""
    if not scores:
        return {"start": 0.0, "end": 0.0, "average": 0.0, "trend": "flat", "points": []}
    start, end = scores[0], scores[-1]
    average = sum(scores) / len(scores)
    delta = end - start
    trend = "improving" if delta > 0.15 else "declining" if delta < -0.15 else "flat"
    return {
        "start": round(start, 3),
        "end": round(end, 3),
        "average": round(average, 3),
        "trend": trend,
        "points": [round(s, 3) for s in scores],
    }
