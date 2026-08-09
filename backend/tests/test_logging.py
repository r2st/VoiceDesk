"""PII masking in structured logs (design doc §8.1)."""

from __future__ import annotations

import logging

from app.core.logging import (
    PIIMaskingFilter,
    configure_logging,
    get_logger,
    mask_email,
    mask_phone,
    mask_text,
)


class TestMaskPhone:
    def test_masks_all_but_the_last_four_digits(self):
        assert mask_phone("+919876543210") == "+9198****3210"

    def test_empty_value_returns_empty_string(self):
        assert mask_phone("") == ""
        assert mask_phone(None) == ""

    def test_short_value_is_fully_masked(self):
        assert mask_phone("12") == "**"

    def test_number_without_a_plus_prefix_is_not_given_one(self):
        assert not mask_phone("9876543210").startswith("+")


class TestMaskEmail:
    def test_masks_the_local_part_but_keeps_the_domain(self):
        assert mask_email("owner@sunrise.test") == "o****@sunrise.test"

    def test_empty_value_returns_empty_string(self):
        assert mask_email("") == ""
        assert mask_email(None) == ""

    def test_value_without_at_sign_is_returned_unmasked(self):
        assert mask_email("not-an-email") == "not-an-email"

    def test_single_character_local_part(self):
        assert mask_email("a@test.com") == "a*@test.com"


class TestMaskText:
    def test_masks_a_phone_number_embedded_in_a_sentence(self):
        result = mask_text("Called +919876543210 about the appointment")
        assert "9876543210" not in result
        assert result == "Called +91******3210 about the appointment"

    def test_masks_an_email_embedded_in_a_sentence(self):
        result = mask_text("Reach owner@sunrise.test for details")
        assert "owner@sunrise.test" not in result
        assert "@sunrise.test" in result

    def test_text_without_pii_is_unchanged(self):
        assert mask_text("No sensitive data here") == "No sensitive data here"


class TestPIIMaskingFilter:
    def test_masks_the_rendered_message_in_place(self):
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="Call from +919876543210",
            args=(),
            exc_info=None,
        )
        assert PIIMaskingFilter().filter(record) is True
        assert "9876543210" not in record.msg
        assert record.args == ()

    def test_message_without_pii_is_left_alone(self):
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="Nothing sensitive",
            args=(),
            exc_info=None,
        )
        original = record.msg
        assert PIIMaskingFilter().filter(record) is True
        assert record.msg == original

    def test_a_broken_record_still_passes_through(self):
        """``getMessage`` can raise on malformed args; the record must not be dropped."""
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="%s %s",
            args=("only-one",),
            exc_info=None,
        )
        assert PIIMaskingFilter().filter(record) is True


class TestConfigureLogging:
    def test_installs_a_masking_handler_on_the_root_logger(self):
        try:
            configure_logging(level="DEBUG")
            root = logging.getLogger()
            assert len(root.handlers) == 1
            assert any(
                isinstance(f, PIIMaskingFilter) for f in root.handlers[0].filters
            )
            assert root.level == logging.DEBUG
        finally:
            root = logging.getLogger()
            root.handlers = []

    def test_noisy_third_party_loggers_are_quieted(self):
        try:
            configure_logging()
            assert logging.getLogger("httpx").level == logging.WARNING
            assert logging.getLogger("uvicorn.access").level == logging.WARNING
        finally:
            logging.getLogger().handlers = []


def test_get_logger_returns_a_named_logger():
    logger = get_logger("voicedesk.test")
    assert logger.name == "voicedesk.test"
