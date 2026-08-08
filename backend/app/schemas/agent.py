"""Voice agent and intent schemas."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, Field, field_validator

from app.models.enums import AgentStatus, AgentUseCase, IntentActionType, Language
from app.schemas.common import ORMModel


class VoiceAgentBase(BaseModel):
    name: Annotated[str, Field(min_length=2, max_length=120)]
    description: Annotated[str | None, Field(default=None, max_length=500)] = None
    use_case: AgentUseCase = AgentUseCase.CUSTOMER_SUPPORT
    language: Language = Language.HINDI
    supported_languages: list[Language] = Field(default_factory=list)
    voice_id: Annotated[str, Field(max_length=80)] = "azure:hi-IN-SwaraNeural"
    persona: Annotated[str, Field(max_length=8000)] = ""
    greeting: Annotated[str | None, Field(default=None, max_length=2000)] = None
    fallback_message: Annotated[str | None, Field(default=None, max_length=2000)] = None
    max_call_duration_sec: Annotated[int, Field(ge=30, le=3600)] = 600
    handoff_confidence_threshold: Annotated[float, Field(ge=0.0, le=1.0)] = 0.70
    whatsapp_handoff_enabled: bool = True
    recording_enabled: bool = True

    @field_validator("supported_languages")
    @classmethod
    def _dedupe(cls, value: list[Language]) -> list[Language]:
        seen: list[Language] = []
        for lang in value:
            if lang not in seen:
                seen.append(lang)
        return seen


class VoiceAgentCreate(VoiceAgentBase):
    status: AgentStatus = AgentStatus.DRAFT
    #: Omit to receive the default starter flow.
    flow_json: dict | None = None


class VoiceAgentUpdate(BaseModel):
    name: Annotated[str | None, Field(default=None, min_length=2, max_length=120)] = None
    description: Annotated[str | None, Field(default=None, max_length=500)] = None
    use_case: AgentUseCase | None = None
    status: AgentStatus | None = None
    language: Language | None = None
    supported_languages: list[Language] | None = None
    voice_id: Annotated[str | None, Field(default=None, max_length=80)] = None
    persona: Annotated[str | None, Field(default=None, max_length=8000)] = None
    greeting: Annotated[str | None, Field(default=None, max_length=2000)] = None
    fallback_message: Annotated[str | None, Field(default=None, max_length=2000)] = None
    max_call_duration_sec: Annotated[int | None, Field(default=None, ge=30, le=3600)] = None
    handoff_confidence_threshold: Annotated[float | None, Field(default=None, ge=0.0, le=1.0)] = (
        None
    )
    whatsapp_handoff_enabled: bool | None = None
    recording_enabled: bool | None = None


class FlowUpdate(BaseModel):
    flow_json: dict


class VoiceAgentOut(ORMModel):
    id: uuid.UUID
    business_id: uuid.UUID
    name: str
    description: str | None
    use_case: AgentUseCase
    status: AgentStatus
    language: Language
    supported_languages: list[str]
    voice_id: str
    persona: str
    greeting: str | None
    fallback_message: str | None
    flow_json: dict
    flow_version: int
    max_call_duration_sec: int
    handoff_confidence_threshold: float
    whatsapp_handoff_enabled: bool
    recording_enabled: bool
    created_at: datetime
    updated_at: datetime


class FlowValidationResult(BaseModel):
    valid: bool
    node_count: int = 0
    terminal_nodes: list[str] = Field(default_factory=list)
    errors: list[dict] = Field(default_factory=list)


class IntentBase(BaseModel):
    name: Annotated[str, Field(min_length=2, max_length=80, pattern=r"^[a-z][a-z0-9_]*$")]
    description: Annotated[str, Field(max_length=500)] = ""
    sample_phrases: Annotated[list[str], Field(max_length=50)] = Field(default_factory=list)
    action_type: IntentActionType = IntentActionType.NONE
    parameters_json: dict = Field(default_factory=dict)
    is_active: bool = True
    priority: Annotated[int, Field(ge=1, le=1000)] = 100


class IntentCreate(IntentBase):
    #: Scope the intent to one agent, or leave null for business-wide.
    agent_id: uuid.UUID | None = None


class IntentUpdate(BaseModel):
    description: Annotated[str | None, Field(default=None, max_length=500)] = None
    sample_phrases: Annotated[list[str] | None, Field(default=None, max_length=50)] = None
    action_type: IntentActionType | None = None
    parameters_json: dict | None = None
    is_active: bool | None = None
    priority: Annotated[int | None, Field(default=None, ge=1, le=1000)] = None


class IntentOut(ORMModel):
    id: uuid.UUID
    business_id: uuid.UUID
    agent_id: uuid.UUID | None
    name: str
    description: str
    sample_phrases: list[str]
    action_type: IntentActionType
    parameters_json: dict
    is_active: bool
    priority: int
    created_at: datetime
