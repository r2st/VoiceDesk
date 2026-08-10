"""Telephony: phone numbers, call logs, transcripts, recordings and handoffs."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, SoftDeleteMixin, TimestampMixin, UUIDPrimaryKeyMixin
from app.db.types import GUID, JSONBType
from app.models.enums import (
    CallResolution,
    CallStatus,
    HandoffStatus,
    Language,
    PhoneNumberStatus,
    QualityGrade,
    SpeakerRole,
    TelephonyProvider,
    VoicemailStatus,
)


class PhoneNumber(Base, UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin):
    """A telephony number provisioned to a business and bound to an agent."""

    __tablename__ = "phone_numbers"

    business_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False, index=True
    )
    agent_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("voice_agents.id", ondelete="SET NULL"), nullable=True, index=True
    )
    number: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    provider: Mapped[str] = mapped_column(
        String(20), default=TelephonyProvider.MOCK, nullable=False
    )
    provider_number_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    region: Mapped[str | None] = mapped_column(String(80), nullable=True)
    status: Mapped[str] = mapped_column(
        String(20), default=PhoneNumberStatus.PROVISIONING, nullable=False
    )
    inbound_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    outbound_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    monthly_rent_paise: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    __table_args__ = (
        # Partial: a released number returns to the provider's pool and may
        # legitimately be provisioned again later.
        Index(
            "uq_phone_numbers_number_provider_active",
            "number",
            "provider",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
            sqlite_where=text("deleted_at IS NULL"),
        ),
        Index("ix_phone_numbers_business_status", "business_id", "status", "deleted_at"),
    )


class CallLog(Base, UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin):
    """One phone call, inbound or outbound."""

    __tablename__ = "call_logs"

    business_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False, index=True
    )
    agent_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("voice_agents.id", ondelete="SET NULL"), nullable=True, index=True
    )
    phone_number_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("phone_numbers.id", ondelete="SET NULL"), nullable=True
    )

    direction: Mapped[str] = mapped_column(String(10), nullable=False)
    status: Mapped[str] = mapped_column(String(30), default=CallStatus.QUEUED, nullable=False)
    caller_number: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    callee_number: Mapped[str] = mapped_column(String(20), nullable=False)

    provider: Mapped[str] = mapped_column(
        String(20), default=TelephonyProvider.MOCK, nullable=False
    )
    #: Provider-side call identifier; unique so webhook replays are idempotent.
    provider_call_id: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)

    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_sec: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: Rounded-up billable minutes; the billing meter is the sole writer.
    billable_minutes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cost_paise: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    language: Mapped[str | None] = mapped_column(String(5), nullable=True)
    detected_languages: Mapped[list] = mapped_column(JSONBType, default=list, nullable=False)
    sentiment: Mapped[str | None] = mapped_column(String(20), nullable=True)
    sentiment_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    resolution: Mapped[str] = mapped_column(
        String(20), default=CallResolution.PENDING, nullable=False
    )
    primary_intent: Mapped[str | None] = mapped_column(String(80), nullable=True)
    avg_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: TRAI compliance markers (design doc §8.2).
    dnd_checked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    consent_announced: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    opted_out: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    metadata_json: Mapped[dict] = mapped_column(JSONBType, default=dict, nullable=False)

    conversations: Mapped[list[Conversation]] = relationship(
        back_populates="call", lazy="noload", order_by="Conversation.turn_index"
    )
    recording: Mapped[CallRecording | None] = relationship(back_populates="call", lazy="noload")
    voicemail: Mapped[Voicemail | None] = relationship(back_populates="call", lazy="noload")

    __table_args__ = (
        Index("ix_call_logs_business_created", "business_id", "created_at"),
        Index("ix_call_logs_business_status", "business_id", "status", "deleted_at"),
        UniqueConstraint(
            "provider", "provider_call_id", name="uq_call_logs_provider_provider_call_id"
        ),
    )

    @property
    def is_terminal(self) -> bool:
        return self.status in {s.value for s in CallStatus.terminal()}


class Conversation(Base, UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin):
    """A single turn of dialogue within a call."""

    __tablename__ = "conversations"

    business_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False, index=True
    )
    call_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("call_logs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    turn_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    role: Mapped[str] = mapped_column(String(20), default=SpeakerRole.CALLER, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    language: Mapped[str | None] = mapped_column(String(5), default=Language.HINDI, nullable=True)
    #: ASR or LLM confidence for this turn, 0.0–1.0.
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    sentiment: Mapped[str | None] = mapped_column(String(20), nullable=True)
    detected_intent: Mapped[str | None] = mapped_column(String(80), nullable=True)
    #: Flow node that produced or consumed this turn.
    flow_node_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    model_used: Mapped[str | None] = mapped_column(String(120), nullable=True)
    metadata_json: Mapped[dict] = mapped_column(JSONBType, default=dict, nullable=False)

    call: Mapped[CallLog] = relationship(back_populates="conversations", lazy="noload")

    __table_args__ = (
        UniqueConstraint("call_id", "turn_index", name="uq_conversations_call_id_turn_index"),
        Index("ix_conversations_business_call", "business_id", "call_id"),
    )


class CallRecording(Base, UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin):
    """Reference to an encrypted audio object in S3/MinIO."""

    __tablename__ = "call_recordings"

    business_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False, index=True
    )
    call_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("call_logs.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    storage_path: Mapped[str] = mapped_column(String(500), nullable=False)
    storage_bucket: Mapped[str] = mapped_column(String(120), nullable=False)
    content_type: Mapped[str] = mapped_column(String(60), default="audio/ogg", nullable=False)
    format: Mapped[str] = mapped_column(String(20), default="opus", nullable=False)
    duration_sec: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    encrypted: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    encryption_algorithm: Mapped[str] = mapped_column(
        String(30), default="AES-256-GCM", nullable=False
    )
    checksum_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: Set from the business retention policy (default 90 days).
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    purged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    call: Mapped[CallLog] = relationship(back_populates="recording", lazy="noload")


class CallQualityMetric(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One media-quality sample reported for a live or completed call.

    Samples are appended by the media edge every few seconds while a call is
    in progress. Rows are never updated — the grade is derived from the raw
    numbers at read time, so a threshold change applies retroactively without
    a backfill.
    """

    __tablename__ = "call_quality_metrics"

    business_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False, index=True
    )
    call_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("call_logs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    sampled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    jitter_ms: Mapped[float] = mapped_column(Float, nullable=False)
    packet_loss_pct: Mapped[float] = mapped_column(Float, nullable=False)
    #: Mean Opinion Score estimate (1.0-5.0), when the media edge computes one.
    mos_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    grade: Mapped[str] = mapped_column(String(10), default=QualityGrade.GOOD, nullable=False)
    #: Where the sample came from, e.g. "media_edge", "sip_probe".
    source: Mapped[str] = mapped_column(String(40), default="media_edge", nullable=False)

    __table_args__ = (
        Index("ix_call_quality_metrics_call_sampled", "call_id", "sampled_at"),
        Index("ix_call_quality_metrics_business_created", "business_id", "created_at"),
    )


class Voicemail(Base, UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin):
    """A message a caller left instead of reaching an agent.

    Audio is stored encrypted the same way call recordings are (design doc
    §2.4); the row also tracks transcription state and whether an operator has
    listened to it yet, which is what the dashboard's unread badge counts.
    """

    __tablename__ = "voicemails"

    business_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False, index=True
    )
    call_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("call_logs.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    phone_number_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("phone_numbers.id", ondelete="SET NULL"), nullable=True
    )
    caller_number: Mapped[str] = mapped_column(String(20), nullable=False, index=True)

    storage_path: Mapped[str] = mapped_column(String(500), nullable=False)
    storage_bucket: Mapped[str] = mapped_column(String(120), nullable=False)
    content_type: Mapped[str] = mapped_column(String(60), default="audio/ogg", nullable=False)
    format: Mapped[str] = mapped_column(String(20), default="opus", nullable=False)
    duration_sec: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    checksum_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)

    status: Mapped[str] = mapped_column(
        String(20), default=VoicemailStatus.PENDING, nullable=False
    )
    transcript: Mapped[str | None] = mapped_column(Text, nullable=True)
    transcribed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    listened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    listened_by: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    call: Mapped[CallLog] = relationship(back_populates="voicemail", lazy="noload")

    __table_args__ = (
        Index("ix_voicemails_business_created", "business_id", "created_at"),
    )

    @property
    def is_unheard(self) -> bool:
        return self.listened_at is None and self.deleted_at is None


class WhatsAppHandoff(Base, UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin):
    """Record of a voice-to-WhatsApp escalation (design doc §4.5)."""

    __tablename__ = "whatsapp_handoffs"

    business_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False, index=True
    )
    call_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("call_logs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    to_number: Mapped[str] = mapped_column(String(20), nullable=False)
    reason: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default=HandoffStatus.PENDING, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    #: Full transcript context handed to the WhatsApp channel.
    context_json: Mapped[dict] = mapped_column(JSONBType, default=dict, nullable=False)
    provider: Mapped[str] = mapped_column(String(30), default="mock", nullable=False)
    provider_message_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    confidence_at_handoff: Mapped[float | None] = mapped_column(Float, nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(500), nullable=True)


class CallTakeover(Base, UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin):
    """A supervisor taking control of a live call from the AI (design doc §4.3).

    Rows are kept after release: who joined which call, when, and how much they
    said is exactly the audit trail a compliance review asks for. The open row
    (``ended_at IS NULL``) is the current controller.
    """

    __tablename__ = "call_takeovers"

    business_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False, index=True
    )
    call_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("call_logs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    supervisor_user_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: Free text from the supervisor ("caller upset", "AI looping").
    reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: Turns the supervisor spoke while in control.
    turns_spoken: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: Set when control was handed back to the AI rather than ended with the call.
    returned_to_ai: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    __table_args__ = (
        # Two supervisors talking over each other on one call is not a state
        # the engine can resolve, so the database refuses it outright.
        Index(
            "uq_call_takeovers_active_call",
            "call_id",
            unique=True,
            postgresql_where=text("ended_at IS NULL AND deleted_at IS NULL"),
            sqlite_where=text("ended_at IS NULL AND deleted_at IS NULL"),
        ),
        Index("ix_call_takeovers_business_started", "business_id", "started_at"),
    )

    @property
    def is_active(self) -> bool:
        return self.ended_at is None and self.deleted_at is None


class DNDRegistry(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Local cache of TRAI DND status plus per-business opt-outs."""

    __tablename__ = "dnd_registry"

    #: NULL means a platform-wide (national registry) entry.
    business_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("businesses.id", ondelete="CASCADE"), nullable=True, index=True
    )
    phone_number: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    is_dnd: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    source: Mapped[str] = mapped_column(String(40), default="national_registry", nullable=False)
    checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("business_id", "phone_number", name="uq_dnd_business_phone"),
    )
