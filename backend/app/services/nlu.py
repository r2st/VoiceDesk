"""Language detection, intent classification and sentiment analysis.

Each function tries a cheap deterministic heuristic first and only escalates to
the LLM when the heuristic is not confident. On a phone call, a script-based
language guess is both faster and more reliable than a model round-trip, and it
keeps the latency budget (design doc §2.3) intact.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

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


# --------------------------------------------------------------------------- #
# Appointment slot extraction
# --------------------------------------------------------------------------- #
#: Relative day words, in the languages the pipeline actually hears on a call.
#: Order matters — "day after tomorrow" must be tried before "tomorrow".
RELATIVE_DAYS: tuple[tuple[str, int], ...] = (
    ("day after tomorrow", 2),
    ("parson", 2),
    ("parso", 2),
    ("परसों", 2),
    ("tomorrow", 1),
    ("kal", 1),
    ("कल", 1),
    ("today", 0),
    ("aaj", 0),
    ("आज", 0),
    ("abhi", 0),
    ("अभी", 0),
)

WEEKDAY_WORDS: dict[str, int] = {
    "monday": 0,
    "somvar": 0,
    "सोमवार": 0,
    "tuesday": 1,
    "mangalvar": 1,
    "मंगलवार": 1,
    "wednesday": 2,
    "budhvar": 2,
    "बुधवार": 2,
    "thursday": 3,
    "guruvar": 3,
    "गुरुवार": 3,
    "friday": 4,
    "shukravar": 4,
    "शुक्रवार": 4,
    "saturday": 5,
    "shanivar": 5,
    "शनिवार": 5,
    "sunday": 6,
    "ravivar": 6,
    "रविवार": 6,
}

#: Words that pin an otherwise ambiguous hour to morning or afternoon. "am" and
#: "pm" are deliberately absent — the regex below captures them as a marker, and
#: matching them as substrings would read "sh(am)" as a morning.
MORNING_MARKERS = ("morning", "subah", "सुबह")
AFTERNOON_MARKERS = (
    "afternoon",
    "evening",
    "night",
    "dopahar",
    "shaam",
    "sham",
    "raat",
    "दोपहर",
    "शाम",
    "रात",
)

#: "3 pm", "11 baje", "10:30", "साढ़े" is deliberately out of scope — the LLM
#: fallback handles anything this does not.
_TIME_RE = re.compile(
    r"\b(?P<hour>\d{1,2})(?:[:.](?P<minute>\d{2}))?\s*"
    r"(?P<marker>am|a\.m\.|pm|p\.m\.|baje|बजे|o'?clock)?\b",
    re.IGNORECASE,
)

#: Below this hour, a bare number on a business call means the afternoon:
#: "come at 3" is 15:00, never 03:00.
AFTERNOON_ROLLOVER_HOUR = 8


@dataclass(frozen=True, slots=True)
class DateTimeGuess:
    """A resolved appointment time, or ``None`` when nothing could be parsed."""

    when: datetime | None
    confidence: float
    method: str

    @property
    def resolved(self) -> bool:
        return self.when is not None


def parse_datetime_heuristic(
    text: str, *, reference: datetime, timezone: str = "Asia/Kolkata"
) -> DateTimeGuess:
    """Resolve "kal subah 11 baje" against ``reference``, without a model call.

    A day alone is not enough — a booking needs a time — so a phrase that names
    only a day returns unresolved and lets the caller be asked for the hour.
    """
    if not text or not text.strip():
        return DateTimeGuess(None, 0.0, "empty")

    zone = ZoneInfo(timezone)
    local_reference = reference.astimezone(zone)
    lowered = text.lower()

    day_offset, day_method = _relative_day(lowered, local_reference)
    moment = _clock_time(lowered)
    if moment is None:
        return DateTimeGuess(None, 0.0, "no-time")

    target = (local_reference + timedelta(days=day_offset)).replace(
        hour=moment.hour, minute=moment.minute, second=0, microsecond=0
    )
    # "at 10" said at 18:00 with no day word means tomorrow, not the past.
    if day_method == "implicit-today" and target <= local_reference:
        target += timedelta(days=1)
        day_method = "implicit-tomorrow"

    return DateTimeGuess(target, 0.85 if day_method != "implicit-today" else 0.7, day_method)


def _relative_day(lowered: str, reference: datetime) -> tuple[int, str]:
    for word, offset in RELATIVE_DAYS:
        if word in lowered:
            return offset, f"relative:{word}"

    for word, weekday in WEEKDAY_WORDS.items():
        if word in lowered:
            ahead = (weekday - reference.weekday()) % 7
            return ahead or 7, f"weekday:{word}"

    return 0, "implicit-today"


def _clock_time(lowered: str) -> time | None:
    for match in _TIME_RE.finditer(lowered):
        hour = int(match.group("hour"))
        minute = int(match.group("minute") or 0)
        if hour > 24 or minute > 59:
            continue
        marker = (match.group("marker") or "").lower()
        if marker.startswith("p") and hour < 12:
            hour += 12
        elif marker.startswith("a") and hour == 12:
            hour = 0
        elif not marker.startswith(("a", "p")):
            hour = _disambiguate_hour(hour, lowered)
        return time(hour % 24, minute)
    return None


def _disambiguate_hour(hour: int, lowered: str) -> int:
    if hour >= 13:
        return hour
    if any(marker in lowered for marker in MORNING_MARKERS):
        return hour
    if any(marker in lowered for marker in AFTERNOON_MARKERS) and hour < 12:
        return hour + 12
    if hour < AFTERNOON_ROLLOVER_HOUR:
        return hour + 12
    return hour


async def parse_datetime_phrase(
    text: str,
    *,
    reference: datetime,
    timezone: str = "Asia/Kolkata",
    client: OpenRouterClient | None = None,
) -> DateTimeGuess:
    """Heuristic first; ask the LLM only for phrasings the regex cannot reach."""
    guess = parse_datetime_heuristic(text, reference=reference, timezone=timezone)
    if guess.resolved:
        return guess
    if not text or not text.strip():
        return guess

    zone = ZoneInfo(timezone)
    local_reference = reference.astimezone(zone)
    try:
        llm = client or get_llm_client()
        data, _ = await llm.chat_json(
            [
                LLMMessage(
                    role="system",
                    content=(
                        "Extract the appointment date and time the caller asked for. "
                        f"Right now it is {local_reference:%Y-%m-%d %H:%M} "
                        f"({local_reference:%A}) in {timezone}. "
                        'Reply with JSON: {"date": "YYYY-MM-DD", "time": "HH:MM" '
                        '(24-hour), "confidence": 0.0-1.0}. Use null for either field '
                        "if the caller did not say it. Never guess a time the caller "
                        "did not mention."
                    ),
                ),
                LLMMessage(role="user", content=text[:500]),
            ],
            max_tokens=80,
        )
    except Exception as exc:
        logger.warning("Appointment time extraction failed: %s", exc)
        return DateTimeGuess(None, 0.0, "error")

    try:
        day = date.fromisoformat(str(data["date"]))
        moment = time.fromisoformat(str(data["time"]))
    except (KeyError, TypeError, ValueError):
        return DateTimeGuess(None, 0.0, "llm-incomplete")

    when = datetime.combine(day, moment.replace(second=0, microsecond=0), tzinfo=zone)
    confidence = max(0.0, min(1.0, float(data.get("confidence", 0.6))))
    return DateTimeGuess(when, confidence, "llm")
