"""Enumerations shared across models, schemas and services."""

from __future__ import annotations

from enum import StrEnum


class PlanTier(StrEnum):
    STARTER = "starter"
    GROWTH = "growth"
    BUSINESS = "business"
    ENTERPRISE = "enterprise"


class BusinessStatus(StrEnum):
    TRIAL = "trial"
    ACTIVE = "active"
    SUSPENDED = "suspended"
    CANCELLED = "cancelled"


class UserRole(StrEnum):
    OWNER = "owner"
    ADMIN = "admin"
    SUPERVISOR = "supervisor"
    VIEWER = "viewer"


class Language(StrEnum):
    """Languages supported by the voice pipeline (design doc §4.2)."""

    HINDI = "hi"
    ENGLISH = "en"
    TAMIL = "ta"
    TELUGU = "te"
    MARATHI = "mr"
    BENGALI = "bn"
    KANNADA = "kn"


class AgentStatus(StrEnum):
    DRAFT = "draft"
    ACTIVE = "active"
    PAUSED = "paused"


class AgentUseCase(StrEnum):
    APPOINTMENT_BOOKING = "appointment_booking"
    PAYMENT_REMINDER = "payment_reminder"
    LEAD_QUALIFICATION = "lead_qualification"
    ORDER_STATUS = "order_status"
    CUSTOMER_SUPPORT = "customer_support"


class PhoneNumberStatus(StrEnum):
    PROVISIONING = "provisioning"
    ACTIVE = "active"
    RELEASED = "released"
    FAILED = "failed"


class TelephonyProvider(StrEnum):
    EXOTEL = "exotel"
    KNOWLARITY = "knowlarity"
    MOCK = "mock"


class CallDirection(StrEnum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"


class CallStatus(StrEnum):
    QUEUED = "queued"
    RINGING = "ringing"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    NO_ANSWER = "no_answer"
    BUSY = "busy"
    FAILED = "failed"
    CANCELLED = "cancelled"
    BLOCKED_DND = "blocked_dnd"
    BLOCKED_HOURS = "blocked_calling_hours"

    @classmethod
    def terminal(cls) -> set[CallStatus]:
        return {
            cls.COMPLETED,
            cls.NO_ANSWER,
            cls.BUSY,
            cls.FAILED,
            cls.CANCELLED,
            cls.BLOCKED_DND,
            cls.BLOCKED_HOURS,
        }

    @classmethod
    def billable(cls) -> set[CallStatus]:
        return {cls.COMPLETED}


class CallResolution(StrEnum):
    RESOLVED = "resolved"
    UNRESOLVED = "unresolved"
    ESCALATED = "escalated"
    HANDED_OFF = "handed_off"
    PENDING = "pending"


class Sentiment(StrEnum):
    POSITIVE = "positive"
    NEUTRAL = "neutral"
    NEGATIVE = "negative"


class SpeakerRole(StrEnum):
    CALLER = "caller"
    AGENT = "agent"
    SYSTEM = "system"
    HUMAN = "human"


class IntentActionType(StrEnum):
    BOOK_APPOINTMENT = "book_appointment"
    CHECK_ORDER_STATUS = "check_order_status"
    COLLECT_PAYMENT = "collect_payment"
    QUALIFY_LEAD = "qualify_lead"
    TRANSFER_HUMAN = "transfer_human"
    WHATSAPP_HANDOFF = "whatsapp_handoff"
    WEBHOOK = "webhook"
    END_CALL = "end_call"
    NONE = "none"


class HandoffReason(StrEnum):
    LOW_CONFIDENCE = "low_confidence"
    CALLER_REQUEST = "caller_request"
    DOCUMENT_REQUIRED = "document_required"
    AGENT_ERROR = "agent_error"
    UNSUPPORTED_INTENT = "unsupported_intent"


class HandoffStatus(StrEnum):
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"
    ACKNOWLEDGED = "acknowledged"
