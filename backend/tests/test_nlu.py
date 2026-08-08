"""Language detection, intent classification and sentiment (design doc §4.2)."""

from __future__ import annotations

import pytest

from app.models.enums import Language, Sentiment
from app.services import nlu


class TestLanguageHeuristic:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("मुझे अपॉइंटमेंट चाहिए", Language.HINDI),
            ("எனக்கு ஒரு நேரம் வேண்டும்", Language.TAMIL),
            ("నాకు అపాయింట్‌మెంట్ కావాలి", Language.TELUGU),
            ("আমার একটি অ্যাপয়েন্টমেন্ট দরকার", Language.BENGALI),
            ("ನನಗೆ ಅಪಾಯಿಂಟ್‌ಮೆಂಟ್ ಬೇಕು", Language.KANNADA),
            ("I would like to book an appointment", Language.ENGLISH),
        ],
    )
    def test_detects_by_script(self, text, expected):
        assert nlu.detect_language_heuristic(text).language is expected

    def test_marathi_is_disambiguated_from_hindi(self):
        """Both use Devanagari, so Marathi needs marker words to be told apart."""
        guess = nlu.detect_language_heuristic("मला अपॉइंटमेंट पाहिजे आहे")
        assert guess.language is Language.MARATHI

    def test_hinglish_code_switching_is_detected_as_hindi(self):
        """Design doc §4.2: mixed Hindi-English must be handled natively."""
        guess = nlu.detect_language_heuristic("mujhe appointment chahiye kal ke liye")
        assert guess.language is Language.HINDI
        assert guess.method.startswith("hinglish")

    def test_plain_english_is_not_mistaken_for_hinglish(self):
        guess = nlu.detect_language_heuristic("Can you confirm the appointment time please")
        assert guess.language is Language.ENGLISH

    def test_empty_input_returns_zero_confidence(self):
        guess = nlu.detect_language_heuristic("   ")
        assert guess.confidence == 0.0

    def test_script_detection_is_high_confidence(self):
        assert nlu.detect_language_heuristic("வணக்கம்").confidence >= 0.9


class TestDetectLanguage:
    async def test_confident_heuristic_skips_the_llm(self, fake_llm):
        guess = await nlu.detect_language("மருத்துவரை பார்க்க வேண்டும்")
        assert guess.language is Language.TAMIL
        assert fake_llm.calls == [], "a confident guess must not cost an LLM call"

    async def test_weak_guess_escalates_to_the_llm(self, fake_llm):
        # Digits alone carry no script and no keywords, so the heuristic bottoms
        # out at 0.30 confidence and the LLM is consulted.
        fake_llm.queue_json({"language": "hi", "confidence": 0.9})
        await nlu.detect_language("123 456")
        assert fake_llm.calls, "a weak guess should be escalated"

    async def test_result_is_constrained_to_the_allowed_languages(self, fake_llm):
        """An agent configured for Hindi/English must never answer in Tamil."""
        guess = await nlu.detect_language(
            "வணக்கம்", allowed=[Language.HINDI, Language.ENGLISH]
        )
        assert guess.language in {Language.HINDI, Language.ENGLISH}

    async def test_llm_failure_falls_back_to_the_heuristic(self, fake_llm):
        fake_llm.raise_error = True
        guess = await nlu.detect_language("ok")
        assert guess.language in set(Language)


class TestSentimentHeuristic:
    @pytest.mark.parametrize(
        "text", ["thank you so much", "that is perfect", "बढ़िया", "shukriya bhai"]
    )
    def test_positive(self, text):
        guess = nlu.analyze_sentiment_heuristic(text)
        assert guess.sentiment is Sentiment.POSITIVE
        assert guess.score > 0

    @pytest.mark.parametrize(
        "text", ["this is terrible", "I want a refund", "बकवास", "your service is useless"]
    )
    def test_negative(self, text):
        guess = nlu.analyze_sentiment_heuristic(text)
        assert guess.sentiment is Sentiment.NEGATIVE
        assert guess.score < 0

    def test_neutral(self):
        guess = nlu.analyze_sentiment_heuristic("my appointment is at four")
        assert guess.sentiment is Sentiment.NEUTRAL
        assert guess.score == 0.0

    def test_score_is_bounded(self):
        strong = nlu.analyze_sentiment_heuristic(
            "terrible worst useless complaint refund cheated frustrated disappointed"
        )
        assert -1.0 <= strong.score <= 1.0

    def test_balanced_signals_cancel_to_neutral(self):
        guess = nlu.analyze_sentiment_heuristic("good but terrible")
        assert guess.sentiment is Sentiment.NEUTRAL

    def test_the_stronger_side_wins_when_signals_are_unequal(self):
        # "thank" and "thanks" both match, outweighing the single negative.
        guess = nlu.analyze_sentiment_heuristic("thanks but this is terrible")
        assert guess.sentiment is Sentiment.POSITIVE


class TestAnalyzeSentiment:
    async def test_clear_signal_skips_the_llm(self, fake_llm):
        guess = await nlu.analyze_sentiment("thank you so much")
        assert guess.sentiment is Sentiment.POSITIVE
        assert fake_llm.calls == []

    async def test_neutral_text_escalates(self, fake_llm):
        fake_llm.queue_json({"sentiment": "negative", "score": -0.6})
        guess = await nlu.analyze_sentiment("my appointment is at four")
        assert fake_llm.calls
        assert guess.sentiment is Sentiment.NEGATIVE

    async def test_llm_can_be_disabled(self, fake_llm):
        await nlu.analyze_sentiment("my appointment is at four", use_llm=False)
        assert fake_llm.calls == []

    async def test_empty_text_is_neutral_without_a_call(self, fake_llm):
        guess = await nlu.analyze_sentiment("   ")
        assert guess.sentiment is Sentiment.NEUTRAL
        assert fake_llm.calls == []


class TestClassifyIntent:
    INTENTS = [
        {"name": "book_appointment", "description": "wants a slot", "sample_phrases": []},
        {"name": "check_order", "description": "asks about an order", "sample_phrases": []},
    ]

    async def test_returns_the_matched_intent(self, fake_llm):
        fake_llm.queue_json({"intent": "book_appointment", "confidence": 0.92})
        guess = await nlu.classify_intent("mujhe appointment chahiye", self.INTENTS)

        assert guess.intent == "book_appointment"
        assert guess.confidence == pytest.approx(0.92)

    async def test_a_hallucinated_intent_name_is_discarded(self, fake_llm):
        """The model must not be able to invent intents the business never defined."""
        fake_llm.queue_json({"intent": "order_pizza", "confidence": 0.99})
        guess = await nlu.classify_intent("something", self.INTENTS)

        assert guess.intent is None
        assert guess.method == "llm-no-match"

    async def test_confidence_is_clamped(self, fake_llm):
        fake_llm.queue_json({"intent": "check_order", "confidence": 5.0})
        guess = await nlu.classify_intent("where is my order", self.INTENTS)
        assert guess.confidence == 1.0

    async def test_llm_failure_degrades_gracefully(self, fake_llm):
        fake_llm.raise_error = True
        guess = await nlu.classify_intent("anything", self.INTENTS)

        assert guess.intent is None
        assert guess.method == "error"

    async def test_no_configured_intents_short_circuits(self, fake_llm):
        guess = await nlu.classify_intent("anything", [])
        assert guess.intent is None
        assert fake_llm.calls == []

    async def test_non_dict_parameters_are_ignored(self, fake_llm):
        fake_llm.queue_json(
            {"intent": "book_appointment", "confidence": 0.8, "parameters": "oops"}
        )
        guess = await nlu.classify_intent("book me in", self.INTENTS)
        assert guess.parameters is None


class TestSentimentTrajectory:
    def test_empty_series(self):
        result = nlu.sentiment_trajectory([])
        assert result["points"] == []

    def test_improving_conversation(self):
        result = nlu.sentiment_trajectory([-0.8, -0.4, 0.1, 0.7])
        assert result["trend"] == "improving"
        assert result["end"] > result["start"]

    def test_declining_conversation(self):
        result = nlu.sentiment_trajectory([0.7, 0.2, -0.5, -0.9])
        assert result["trend"] == "declining"

    def test_flat_conversation(self):
        result = nlu.sentiment_trajectory([0.1, 0.1, 0.1])
        assert result["trend"] == "flat"

    def test_small_movement_is_still_flat(self):
        # A 0.15 dead band stops noise reading as a real trend.
        assert nlu.sentiment_trajectory([0.0, 0.1])["trend"] == "flat"

    def test_points_are_preserved_for_charting(self):
        assert nlu.sentiment_trajectory([-0.5, 0.5])["points"] == [-0.5, 0.5]

    def test_average_is_reported(self):
        result = nlu.sentiment_trajectory([-1.0, 1.0])
        assert result["average"] == pytest.approx(0.0)
