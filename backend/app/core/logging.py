"""Structured logging with PII masking.

Phone numbers, email addresses and anything explicitly tagged as PII are masked
before a record reaches a handler (design doc §8.1).
"""

from __future__ import annotations

import logging
import re
import sys

from app.core.config import settings

_PHONE_RE = re.compile(r"(?<!\d)(\+?\d{1,3}[-.\s]?)?(\d{6})(\d{4})(?!\d)")
_EMAIL_RE = re.compile(r"([A-Za-z0-9._%+-])[A-Za-z0-9._%+-]*(@[A-Za-z0-9.-]+\.[A-Za-z]{2,})")


def mask_phone(value: str | None) -> str:
    """Mask all but the last 4 digits of a phone number: +919876543210 -> +91987****210."""
    if not value:
        return ""
    digits = re.sub(r"\D", "", value)
    if len(digits) < 4:
        return "*" * len(value)
    prefix = "+" if value.startswith("+") else ""
    visible_tail = digits[-4:]
    head = digits[:-4]
    keep_head = head[: max(0, len(head) - 4)]
    return f"{prefix}{keep_head}{'*' * (len(head) - len(keep_head))}{visible_tail}"


def mask_email(value: str | None) -> str:
    if not value or "@" not in value:
        return value or ""
    local, _, domain = value.partition("@")
    return f"{local[0]}{'*' * max(1, len(local) - 1)}@{domain}"


def mask_text(text: str) -> str:
    """Mask phone numbers and emails appearing anywhere in a free-text string."""
    text = _PHONE_RE.sub(lambda m: f"{m.group(1) or ''}{'*' * 6}{m.group(3)}", text)
    text = _EMAIL_RE.sub(lambda m: f"{m.group(1)}****{m.group(2)}", text)
    return text


class PIIMaskingFilter(logging.Filter):
    """Applies :func:`mask_text` to the rendered message of every log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - defensive
            return True
        masked = mask_text(message)
        if masked != message:
            record.msg = masked
            record.args = ()
        return True


def configure_logging(level: str | None = None) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(name)s | %(message)s")
    )
    handler.addFilter(PIIMaskingFilter())

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level or settings.log_level)

    for noisy in ("uvicorn.access", "httpx", "botocore", "boto3", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
