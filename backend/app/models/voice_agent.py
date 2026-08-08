"""Voice agents, their conversation flows, and detectable intents."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, Float, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, SoftDeleteMixin, TimestampMixin, UUIDPrimaryKeyMixin
from app.db.types import GUID, JSONBType
from app.models.enums import AgentStatus, AgentUseCase, IntentActionType, Language

if TYPE_CHECKING:
    from app.models.business import Business


class VoiceAgent(Base, UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin):
    """An AI persona that answers or places calls on behalf of a business."""

    __tablename__ = "voice_agents"

    business_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)
    use_case: Mapped[str] = mapped_column(
        String(40), default=AgentUseCase.CUSTOMER_SUPPORT, nullable=False
    )
    status: Mapped[str] = mapped_column(String(20), default=AgentStatus.DRAFT, nullable=False)

    #: Default language; the pipeline still auto-detects and switches at runtime.
    language: Mapped[str] = mapped_column(String(5), default=Language.HINDI, nullable=False)
    #: Additional languages this agent is allowed to switch into.
    supported_languages: Mapped[list] = mapped_column(JSONBType, default=list, nullable=False)
    voice_id: Mapped[str] = mapped_column(String(80), default="azure:hi-IN-SwaraNeural")

    #: System prompt describing tone, role and business rules.
    persona: Mapped[str] = mapped_column(Text, nullable=False, default="")
    greeting: Mapped[str | None] = mapped_column(Text, nullable=True)
    fallback_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: Conversation flow graph — see app.services.flow for the node schema.
    flow_json: Mapped[dict] = mapped_column(JSONBType, default=dict, nullable=False)
    flow_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    max_call_duration_sec: Mapped[int] = mapped_column(Integer, default=600, nullable=False)
    #: Below this LLM confidence the call is handed off (design doc §4.5).
    handoff_confidence_threshold: Mapped[float] = mapped_column(Float, default=0.70, nullable=False)
    whatsapp_handoff_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    recording_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    business: Mapped[Business] = relationship(back_populates="agents", lazy="noload")

    __table_args__ = (
        # Partial: a soft-deleted agent must not hold its name hostage, since
        # rows are never physically removed.
        Index(
            "uq_voice_agents_business_name_active",
            "business_id",
            "name",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
            sqlite_where=text("deleted_at IS NULL"),
        ),
        Index("ix_voice_agents_business_status", "business_id", "status", "deleted_at"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<VoiceAgent {self.name}>"


class Intent(Base, UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin):
    """A caller intent the conversation engine can detect and act on."""

    __tablename__ = "intents"

    business_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False, index=True
    )
    agent_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("voice_agents.id", ondelete="CASCADE"), nullable=True, index=True
    )
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    description: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    #: Example caller utterances used to prime the classifier.
    sample_phrases: Mapped[list] = mapped_column(JSONBType, default=list, nullable=False)
    action_type: Mapped[str] = mapped_column(
        String(40), default=IntentActionType.NONE, nullable=False
    )
    parameters_json: Mapped[dict] = mapped_column(JSONBType, default=dict, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=100, nullable=False)

    __table_args__ = (
        Index(
            "uq_intents_scope_name_active",
            "business_id",
            "agent_id",
            "name",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
            sqlite_where=text("deleted_at IS NULL"),
        ),
    )
