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
    QualityGrade,
    Sentiment,
    SpeakerRole,
    TelephonyProvider,
    VoicemailStatus,
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
# Call quality
# --------------------------------------------------------------------------- #
class QualitySampleRequest(BaseModel):
    """One media-quality reading posted by the edge while a call is live."""

    latency_ms: Annotated[int, Field(ge=0, le=60_000)]
    jitter_ms: Annotated[float, Field(ge=0.0, le=10_000.0)]
    packet_loss_pct: Annotated[float, Field(ge=0.0, le=100.0)]
    mos_score: Annotated[float | None, Field(default=None, ge=1.0, le=5.0)] = None
    source: Annotated[str, Field(max_length=40)] = "media_edge"
    sampled_at: datetime | None = None


class QualitySampleOut(ORMModel):
    id: uuid.UUID
    call_id: uuid.UUID
    sampled_at: datetime
    latency_ms: int
    jitter_ms: float
    packet_loss_pct: float
    mos_score: float | None
    grade: QualityGrade
    source: str


class QualitySummaryOut(BaseModel):
    """Rolled-up quality for a call, for the transcript and the live board."""

    call_id: uuid.UUID
    sample_count: int
    avg_latency_ms: float | None
    max_latency_ms: int | None
    avg_jitter_ms: float | None
    max_jitter_ms: float | None
    avg_packet_loss_pct: float | None
    max_packet_loss_pct: float | None
    worst_grade: QualityGrade | None


# --------------------------------------------------------------------------- #
# Voicemail
# --------------------------------------------------------------------------- #
class VoicemailOut(ORMModel):
    id: uuid.UUID
    business_id: uuid.UUID
    call_id: uuid.UUID
    phone_number_id: uuid.UUID | None
    caller_number: str
    duration_sec: int
    status: VoicemailStatus
    transcript: str | None
    transcribed_at: datetime | None
    listened_at: datetime | None
    is_unheard: bool
    created_at: datetime


class VoicemailTranscriptRequest(BaseModel):
    transcript: Annotated[str, Field(min_length=1, max_length=8000)]


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
