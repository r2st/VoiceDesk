"""BANT scoring for lead qualification (design doc §4.9).

A qualification call collects four things — Budget, Authority, Need, Timeline —
and this module turns the caller's own words into a 0–100 score and a tier a
salesperson can sort a pipeline by.

Scoring is deliberately a pure function of the captured answers and the tenant's
weights. Nothing here calls the LLM or the database, so the same four sentences
always produce the same score: a lead re-scored after a weight change moves for
a reason someone can point at, and a disputed number can be recomputed months
later from the answers still stored on the row.

Configuration lives on ``Business.settings_json`` alongside the calendar::

    {
      "lead_qualification": {
        "weights": {"budget": 30, "authority": 20, "need": 30, "timeline": 20},
        "qualify_at": 60,
        "hot_at": 80,
        "warm_at": 60,
        "currency_floor": 50000
      }
    }
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.models.enums import BANTDimension, LeadTier

#: Platform defaults. Need and budget carry the most weight because a caller
#: with neither is not a lead however senior or urgent they are.
DEFAULT_WEIGHTS: dict[BANTDimension, int] = {
    BANTDimension.BUDGET: 30,
    BANTDimension.AUTHORITY: 20,
    BANTDimension.NEED: 30,
    BANTDimension.TIMELINE: 20,
}

DEFAULT_HOT_AT = 80
DEFAULT_WARM_AT = 60
DEFAULT_QUALIFY_AT = 60

#: A stated budget at or above this (in rupees) scores full marks. Below it the
#: score scales down rather than dropping to zero — a small budget is still a
#: budget, and the tenant can move the floor.
DEFAULT_CURRENCY_FLOOR = 50_000


@dataclass(frozen=True, slots=True)
class QualificationConfig:
    """A tenant's resolved qualification rules."""

    weights: dict[BANTDimension, int]
    hot_at: int
    warm_at: int
    qualify_at: int
    currency_floor: int

    @property
    def total_weight(self) -> int:
        return sum(self.weights.values())


def load_qualification_config(settings_json: dict | None) -> QualificationConfig:
    """Resolve a tenant's scoring rules, falling back to platform defaults.

    Malformed values are ignored rather than raised: a bad weight saved months
    ago must not take a qualification call down mid-conversation.
    """
    raw = settings_json or {}
    candidate = raw.get("lead_qualification")
    options: dict = candidate if isinstance(candidate, dict) else {}

    configured = options.get("weights")
    weights = dict(DEFAULT_WEIGHTS)
    if isinstance(configured, dict):
        for dimension in BANTDimension:
            value = configured.get(dimension.value)
            if isinstance(value, (int, float)) and 0 <= value <= 100:
                weights[dimension] = int(value)

    # Every weight at zero would make the score undefined; fall back rather
    # than divide by nothing.
    if sum(weights.values()) <= 0:
        weights = dict(DEFAULT_WEIGHTS)

    hot = _bounded(options.get("hot_at"), DEFAULT_HOT_AT, 1, 100)
    warm = _bounded(options.get("warm_at"), DEFAULT_WARM_AT, 1, 100)
    # A warm threshold above the hot one would leave a band that is both and
    # neither; the stricter of the two wins.
    warm = min(warm, hot)

    return QualificationConfig(
        weights=weights,
        hot_at=hot,
        warm_at=warm,
        qualify_at=_bounded(options.get("qualify_at"), DEFAULT_QUALIFY_AT, 1, 100),
        currency_floor=_bounded(
            options.get("currency_floor"), DEFAULT_CURRENCY_FLOOR, 1, 1_000_000_000
        ),
    )


def _bounded(value: object, default: int, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return max(low, min(high, int(value)))


# --------------------------------------------------------------------------- #
# Per-dimension signals
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class DimensionScore:
    """One BANT dimension: what was said, what it scored, and why."""

    dimension: BANTDimension
    score: float
    reason: str

    @property
    def percent(self) -> int:
        return round(self.score * 100)


@dataclass(slots=True)
class BantAnswers:
    """The caller's words for each dimension. Any of them may be missing."""

    budget: str | None = None
    authority: str | None = None
    need: str | None = None
    timeline: str | None = None

    def get(self, dimension: BANTDimension) -> str | None:
        return getattr(self, dimension.value)


@dataclass(slots=True)
class Qualification:
    """The scored result for one lead."""

    score: int
    tier: LeadTier
    qualified: bool
    dimensions: dict[BANTDimension, DimensionScore] = field(default_factory=dict)

    def score_for(self, dimension: BANTDimension) -> float:
        found = self.dimensions.get(dimension)
        return found.score if found else 0.0

    def rationale(self) -> dict[str, str]:
        return {d.value: s.reason for d, s in self.dimensions.items()}


# Markers are matched against lowercased text. Hindi and romanised Hindi sit
# alongside English because callers code-switch mid-sentence (design doc §4.2)
# and a qualification call is exactly where they do it.
_NEGATIONS = (
    "not ",
    "no ",
    "n't",
    "never",
    "nahi",
    "nahin",
    "नहीं",
    "बिल्कुल नहीं",
)

_BUDGET_STRONG = (
    "approved",
    "allocated",
    "sanctioned",
    "set aside",
    "budget hai",
    "ready to pay",
    "बजट है",
    "मंज़ूर",
)
_BUDGET_WEAK = ("depends", "not sure", "have to check", "dekhna padega", "pata nahi", "पता नहीं")

_AUTHORITY_STRONG = (
    "i decide",
    "my decision",
    "i am the owner",
    "i'm the owner",
    "founder",
    "director",
    "proprietor",
    "i can approve",
    "main decide",
    "मैं ही",
    "मालिक",
)
_AUTHORITY_PARTIAL = (
    "with my partner",
    "along with",
    "team decides",
    "we decide",
    "jointly",
    "साथ में",
)
_AUTHORITY_WEAK = (
    "my manager",
    "my boss",
    "have to ask",
    "need approval",
    "sir se puchna",
    "boss se",
    "पूछना पड़ेगा",
)

_NEED_STRONG = (
    "urgent",
    "problem",
    "struggling",
    "losing",
    "immediately",
    "badly need",
    "must have",
    "zaroorat",
    "bahut zaroori",
    "ज़रूरत",
    "तुरंत",
)
_NEED_WEAK = ("just looking", "curious", "browsing", "someday", "dekh rahe", "बस देख रहे")

#: Timeline phrases, longest windows last so the first match is the tightest.
_TIMELINE_BANDS: tuple[tuple[float, str, tuple[str, ...]], ...] = (
    (1.0, "wants to start immediately", ("today", "right now", "immediately", "abhi", "आज", "तुरंत")),
    (0.9, "within the week", ("this week", "tomorrow", "kal", "is hafte", "इस हफ़्ते")),
    (0.75, "within the month", ("this month", "next week", "agle hafte", "is mahine", "इस महीने")),
    (0.5, "within the quarter", ("next month", "agle mahine", "quarter", "अगले महीने")),
    (0.25, "six months or more out", ("next year", "six months", "agle saal", "अगले साल")),
    (0.1, "no timeline", ("no rush", "sometime", "koi jaldi nahi", "जल्दी नहीं")),
)

_MONEY = re.compile(
    r"(?:₹|rs\.?|inr)\s*([\d,]+(?:\.\d+)?)\s*(lakh|lakhs|lac|crore|cr|k|thousand)?"
    r"|([\d,]+(?:\.\d+)?)\s*(lakh|lakhs|lac|crore|cr|k|thousand)"
    # A bare figure only counts when it is comma-grouped ("50,000", "2,00,000")
    # and stands alone. Without that guard a phone number read back mid-sentence
    # would score as a nine-figure budget — the one parsing mistake nobody
    # downstream would think to question.
    r"|(?<![\d.,])(\d{1,3}(?:,\d{2,3})+)(?![\d,])()",
    re.IGNORECASE,
)

_MULTIPLIERS = {
    "k": 1_000,
    "thousand": 1_000,
    "lakh": 100_000,
    "lakhs": 100_000,
    "lac": 100_000,
    "crore": 10_000_000,
    "cr": 10_000_000,
}


def parse_amount(text: str) -> float | None:
    """Pull a rupee figure out of spoken text. ``"around 2 lakh"`` -> ``200000``."""
    match = _MONEY.search(text or "")
    if match is None:
        return None
    digits = match.group(1) or match.group(3) or match.group(5)
    unit = (match.group(2) or match.group(4) or "").lower()
    try:
        amount = float(digits.replace(",", ""))
    except (AttributeError, ValueError):
        return None
    return amount * _MULTIPLIERS.get(unit, 1)


def _is_negated(lowered: str) -> bool:
    return any(marker in lowered for marker in _NEGATIONS)


def _contains(lowered: str, markers: tuple[str, ...]) -> bool:
    return any(marker in lowered for marker in markers)


def score_budget(answer: str | None, *, floor: int) -> DimensionScore:
    """A named figure beats a vague reassurance, which beats nothing."""
    dimension = BANTDimension.BUDGET
    if not answer or not answer.strip():
        return DimensionScore(dimension, 0.0, "no budget discussed")

    lowered = answer.lower()
    amount = parse_amount(lowered)
    if amount is not None:
        # A stated figure is the strongest signal there is, even a small one:
        # the caller has thought about money. Scale, floored at 0.4.
        ratio = min(1.0, amount / floor) if floor > 0 else 1.0
        return DimensionScore(
            dimension, max(0.4, ratio), f"stated a budget of about ₹{int(amount):,}"
        )

    if _contains(lowered, _BUDGET_STRONG) and not _is_negated(lowered):
        return DimensionScore(dimension, 0.8, "confirmed budget is available")
    if _contains(lowered, _BUDGET_WEAK) or _is_negated(lowered):
        return DimensionScore(dimension, 0.2, "budget is uncertain")
    return DimensionScore(dimension, 0.4, "budget mentioned without a figure")


def score_authority(answer: str | None) -> DimensionScore:
    dimension = BANTDimension.AUTHORITY
    if not answer or not answer.strip():
        return DimensionScore(dimension, 0.0, "decision maker unknown")

    lowered = answer.lower()
    if _contains(lowered, _AUTHORITY_STRONG):
        return DimensionScore(dimension, 1.0, "speaking to the decision maker")
    if _contains(lowered, _AUTHORITY_PARTIAL):
        return DimensionScore(dimension, 0.6, "shares the decision with others")
    if _contains(lowered, _AUTHORITY_WEAK):
        return DimensionScore(dimension, 0.25, "needs someone else to approve")
    return DimensionScore(dimension, 0.4, "authority unclear from the answer")


def score_need(answer: str | None) -> DimensionScore:
    dimension = BANTDimension.NEED
    if not answer or not answer.strip():
        return DimensionScore(dimension, 0.0, "no need expressed")

    lowered = answer.lower()
    if _contains(lowered, _NEED_WEAK):
        return DimensionScore(dimension, 0.2, "browsing rather than buying")
    if _contains(lowered, _NEED_STRONG):
        return DimensionScore(dimension, 1.0, "has an urgent, stated problem")
    # Anything specific enough to describe is worth more than silence.
    if len(lowered.split()) >= 4:
        return DimensionScore(dimension, 0.6, "described a concrete requirement")
    return DimensionScore(dimension, 0.4, "gave a brief answer on need")


def score_timeline(answer: str | None) -> DimensionScore:
    dimension = BANTDimension.TIMELINE
    if not answer or not answer.strip():
        return DimensionScore(dimension, 0.0, "no timeline given")

    lowered = answer.lower()
    for score, reason, markers in _TIMELINE_BANDS:
        if _contains(lowered, markers):
            return DimensionScore(dimension, score, reason)
    return DimensionScore(dimension, 0.4, "timeline mentioned but not specific")


def qualify(answers: BantAnswers, config: QualificationConfig) -> Qualification:
    """Score a lead across all four dimensions and place it in a tier."""
    scored = (
        score_budget(answers.budget, floor=config.currency_floor),
        score_authority(answers.authority),
        score_need(answers.need),
        score_timeline(answers.timeline),
    )
    dimensions = {item.dimension: item for item in scored}

    weighted = sum(
        dimensions[dimension].score * config.weights.get(dimension, 0)
        for dimension in BANTDimension
    )
    score = round(weighted / config.total_weight * 100)

    tier = (
        LeadTier.HOT
        if score >= config.hot_at
        else LeadTier.WARM
        if score >= config.warm_at
        else LeadTier.COLD
        if score > 0
        else LeadTier.UNQUALIFIED
    )
    return Qualification(
        score=score,
        tier=tier,
        qualified=score >= config.qualify_at,
        dimensions=dimensions,
    )
