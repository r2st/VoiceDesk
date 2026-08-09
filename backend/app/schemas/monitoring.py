"""Live monitoring and supervisor takeover schemas (design doc §4.3)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from pydantic import BaseModel, Field

from app.models.enums import CallDirection, CallStatus, Language, Sentiment
from app.schemas.call import CallOut, ConversationTurnOut
from app.schemas.common import ORMModel


class TakeoverOut(ORMModel):
    id: uuid.UUID
    business_id: uuid.UUID
    call_id: uuid.UUID
    supervisor_user_id: uuid.UUID
    reason: str | None
    started_at: datetime
    ended_at: datetime | None
    turns_spoken: int
    returned_to_ai: bool


class TakeoverRequest(BaseModel):
    reason: Annotated[str | None, Field(default=None, max_length=200)] = None


class ReleaseRequest(BaseModel):
    #: False hands the call straight to hangup rather than back to the AI.
    return_to_ai: bool = True


class SupervisorSayRequest(BaseModel):
    """What the supervisor wants spoken to the caller."""

    text: Annotated[str, Field(min_length=1, max_length=2000)]
    language: Language | None = None


class LiveCallOut(BaseModel):
    """One row on the live board."""

    call_id: uuid.UUID
    agent_id: uuid.UUID | None
    direction: CallDirection
    status: CallStatus
    caller_number: str
    callee_number: str
    language: Language | None
    sentiment: Sentiment | None
    sentiment_score: float | None
    started_at: datetime | None
    answered_at: datetime | None
    #: Seconds since the call was answered; the board's "how long has this been
    #: going" column, computed server-side so every watcher agrees.
    elapsed_sec: int
    turn_count: int
    last_speaker: str | None
    last_utterance: str | None
    takeover: TakeoverOut | None


class CallSnapshotOut(BaseModel):
    """Everything a dashboard needs before it starts streaming a call."""

    call: CallOut
    turns: list[ConversationTurnOut]
    takeover: TakeoverOut | None


class LiveEventOut(BaseModel):
    """The frame shape pushed over the WebSocket."""

    type: str
    business_id: uuid.UUID
    call_id: uuid.UUID | None
    data: dict[str, Any] = Field(default_factory=dict)
    emitted_at: datetime
