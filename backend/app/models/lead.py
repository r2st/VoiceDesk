"""Leads captured and scored during qualification calls (design doc §4.9).

``IntentActionType.QUALIFY_LEAD`` and ``AgentUseCase.LEAD_QUALIFICATION`` name
this flow elsewhere in the platform; this table is where the result lands. Each
BANT dimension keeps both what the caller actually said and the score derived
from it, so a salesperson looking at a 40/100 can see the sentence behind it
rather than having to trust the number.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, SoftDeleteMixin, TimestampMixin, UUIDPrimaryKeyMixin
from app.db.types import GUID, JSONBType
from app.models.enums import CrmPushStatus, LeadSource, LeadStatus, LeadTier


class Lead(Base, UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin):
    """One prospect, scored on the BANT framework."""

    __tablename__ = "leads"

    business_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False, index=True
    )
    agent_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("voice_agents.id", ondelete="SET NULL"), nullable=True, index=True
    )
    #: The qualification call. Kept on SET NULL so purging a call for retention
    #: does not take the lead — and the salesperson's pipeline — with it.
    call_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("call_logs.id", ondelete="SET NULL"), nullable=True, index=True
    )

    contact_name: Mapped[str] = mapped_column(String(200), nullable=False)
    contact_phone: Mapped[str] = mapped_column(String(20), nullable=False)
    contact_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    company: Mapped[str | None] = mapped_column(String(200), nullable=True)
    #: What the caller is actually shopping for.
    interest: Mapped[str | None] = mapped_column(String(300), nullable=True)

    status: Mapped[str] = mapped_column(String(20), default=LeadStatus.NEW, nullable=False)
    tier: Mapped[str] = mapped_column(String(20), default=LeadTier.UNQUALIFIED, nullable=False)
    source: Mapped[str] = mapped_column(String(20), default=LeadSource.VOICE_CALL, nullable=False)
    language: Mapped[str | None] = mapped_column(String(5), nullable=True)

    #: 0–100, weighted across the four dimensions below.
    score: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    budget_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    authority_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    need_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    timeline_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)

    #: What the caller said for each dimension, verbatim.
    budget_answer: Mapped[str | None] = mapped_column(Text, nullable=True)
    authority_answer: Mapped[str | None] = mapped_column(Text, nullable=True)
    need_answer: Mapped[str | None] = mapped_column(Text, nullable=True)
    timeline_answer: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: Why the score came out where it did, in one line per dimension.
    rationale: Mapped[dict] = mapped_column(JSONBType, default=dict, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    qualified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    #: CRM handoff (design doc §4.6). Retried by the worker while it is failed.
    crm_status: Mapped[str] = mapped_column(
        String(20), default=CrmPushStatus.PENDING, nullable=False
    )
    crm_pushed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    crm_reference: Mapped[str | None] = mapped_column(String(160), nullable=True)
    crm_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    crm_error: Mapped[str | None] = mapped_column(String(500), nullable=True)

    metadata_json: Mapped[dict] = mapped_column(JSONBType, default=dict, nullable=False)

    __table_args__ = (
        # The pipeline view filters by status and sorts by score.
        Index("ix_leads_business_status_score", "business_id", "status", "score", "deleted_at"),
        Index("ix_leads_business_phone", "business_id", "contact_phone"),
        # The CRM push job scans for work across every tenant at once, so this
        # one leads on status rather than on business_id.
        Index("ix_leads_crm_status", "crm_status", "deleted_at"),
    )

    @property
    def is_qualified(self) -> bool:
        return self.status == LeadStatus.QUALIFIED

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Lead {self.id} {self.contact_name} {self.tier} {self.score}>"
