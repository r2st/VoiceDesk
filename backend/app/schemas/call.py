"""Call, phone number, recording and handoff schemas."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from pydantic import BaseModel, Field, field_validator

from app.models.enums import (
    CallDirection,
    CallResolution,
    CallStatus,
    HandoffReason,
    HandoffStatus,
    Language,
    PhoneNumberStatus,
    Sentiment,
    SpeakerRole,
    TelephonyProvider,
)
from app.schemas.common import ORMModel, normalize_phone


class InitiateCallRequest(BaseModel):
    agent_id: uuid.UUID
    to_number: str
    #: Which of the business's numbers to dial from; defaults to the agent's.
    from_number_id: uuid.UUID | None = None
    #: ISO timestamp to place the call later; must be inside TRAI calling hours.
    scheduled_at: datetime | None = None
    #: Seeds the conversation state, e.g. ``{"invoice_no": "INV-42"}``.
    variables: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("to_number")
    @classmethod
    def _phone(cls, value: str) -> str:
        return normalize_phone(value)


class CallOut(ORMModel):
    id: uuid.UUID
    business_id: uuid.UUID
    agent_id: uuid.UUID | None
    phone_number_id: uuid.UUID | None
    direction: CallDirection
    status: CallStatus
    caller_number: str
    callee_number: str
    provider: TelephonyProvider
    provider_call_id: str | None
    scheduled_at: datetime | None
    started_at: datetime | None
    answered_at: datetime | None
    ended_at: datetime | None
    duration_sec: int
    billable_minutes: int
    cost_paise: int
    language: Language | None
    detected_languages: list[str]
    sentiment: Sentiment | None
    sentiment_score: float | None
    resolution: CallResolution
    primary_intent: str | None
    avg_confidence: float | None
    summary: str | None
    dnd_checked: bool
    consent_announced: bool
    opted_out: bool
    error_code: str | None
    error_message: str | None
    created_at: datetime


class ConversationTurnOut(ORMModel):
    id: uuid.UUID
    call_id: uuid.UUID
    turn_index: int
    role: SpeakerRole
    content: str
    language: Language | None
    confidence: float | None
    sentiment: Sentiment | None
    detected_intent: str | None
    flow_node_id: str | None
    latency_ms: int | None
    model_used: str | None
    created_at: datetime


class CallDetailOut(CallOut):
    conversations: list[ConversationTurnOut] = Field(default_factory=list)
    has_recording: bool = False
    sentiment_trajectory: dict | None = None


class CallTurnRequest(BaseModel):
    """A transcribed caller utterance handed to the conversation engine."""

    utterance: Annotated[str, Field(min_length=1, max_length=4000)]
    asr_confidence: Annotated[float, Field(ge=0.0, le=1.0)] = 1.0


class CallTurnResponse(BaseModel):
    reply: str
    language: Language
    confidence: float
    node_id: str | None = None
    detected_intent: str | None = None
    sentiment: Sentiment
    sentiment_score: float
    should_end_call: bool
    should_handoff: bool
    handoff_reason: HandoffReason | None = None
    transfer_to: str | None = None
    latency_ms: int
    model_used: str | None = None
    #: A supervisor holds the call; the reply is empty on purpose and the
    #: media edge should wait for the human rather than speak.
    awaiting_human: bool = False


class HangupRequest(BaseModel):
    reason: Annotated[str | None, Field(default=None, max_length=200)] = None


# --------------------------------------------------------------------------- #
# Phone numbers
# --------------------------------------------------------------------------- #
class ProvisionNumberRequest(BaseModel):
    region: Annotated[str | None, Field(default=None, max_length=80)] = None
    #: Request a specific number from the provider's inventory.
    number: str | None = None
    agent_id: uuid.UUID | None = None
    provider: TelephonyProvider | None = None

    @field_validator("number")
    @classmethod
    def _phone(cls, value: str | None) -> str | None:
        return normalize_phone(value) if value else value


class PhoneNumberUpdate(BaseModel):
    agent_id: uuid.UUID | None = None
    inbound_enabled: bool | None = None
    outbound_enabled: bool | None = None


class PhoneNumberOut(ORMModel):
    id: uuid.UUID
    business_id: uuid.UUID
    agent_id: uuid.UUID | None
    number: str
    provider: TelephonyProvider
    provider_number_id: str | None
    region: str | None
    status: PhoneNumberStatus
    inbound_enabled: bool
    outbound_enabled: bool
    monthly_rent_paise: int
    created_at: datetime


# --------------------------------------------------------------------------- #
# Recordings
# --------------------------------------------------------------------------- #
class RecordingOut(ORMModel):
    id: uuid.UUID
    business_id: uuid.UUID
    call_id: uuid.UUID
    storage_path: str
    storage_bucket: str
    content_type: str
    format: str
    duration_sec: int
    size_bytes: int
    encrypted: bool
    encryption_algorithm: str
    checksum_sha256: str | None
    expires_at: datetime | None
    created_at: datetime


class RecordingUrlOut(BaseModel):
    url: str
    expires_in: int
    encrypted: bool
    note: str | None = None


# --------------------------------------------------------------------------- #
# WhatsApp handoff
# --------------------------------------------------------------------------- #
class HandoffRequest(BaseModel):
    reason: HandoffReason = HandoffReason.CALLER_REQUEST
    summary: Annotated[str | None, Field(default=None, max_length=4000)] = None
    to_number: str | None = None

    @field_validator("to_number")
    @classmethod
    def _phone(cls, value: str | None) -> str | None:
        return normalize_phone(value) if value else value


class HandoffOut(ORMModel):
    id: uuid.UUID
    business_id: uuid.UUID
    call_id: uuid.UUID
    to_number: str
    reason: HandoffReason
    status: HandoffStatus
    summary: str
    provider: str
    provider_message_id: str | None
    confidence_at_handoff: float | None
    sent_at: datetime | None
    error_message: str | None
    created_at: datetime


# --------------------------------------------------------------------------- #
# Webhooks
# --------------------------------------------------------------------------- #
class WebhookAck(BaseModel):
    received: bool = True
    call_id: uuid.UUID | None = None
    status: CallStatus | None = None
    #: Set when the event was a duplicate delivery and changed nothing.
    duplicate: bool = False
