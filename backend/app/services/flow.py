"""Conversation flow graph: schema, validation and traversal.

A flow is a directed graph of nodes stored on ``VoiceAgent.flow_json``. The
agent builder (design doc §4.1) edits it visually; the conversation engine
interprets it at runtime.

Shape::

    {
      "start_node": "greet",
      "nodes": [
        {"id": "greet", "type": "message", "text": "...", "next": "ask"},
        {"id": "ask",   "type": "collect", "prompt": "...", "variable": "name",
                        "next": "route"},
        {"id": "route", "type": "intent_branch",
                        "branches": {"book": "book_node"}, "default": "fallback"},
        {"id": "book",  "type": "api_call", "url": "...", "method": "POST",
                        "next": "confirm"},
        {"id": "hand",  "type": "handoff", "channel": "whatsapp"},
        {"id": "bye",   "type": "end", "disposition": "resolved"}
      ]
    }
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, model_validator
from pydantic import ValidationError as PydanticValidationError

from app.core.errors import ValidationError

MAX_NODES = 200


class NodeType(StrEnum):
    MESSAGE = "message"
    COLLECT = "collect"
    INTENT_BRANCH = "intent_branch"
    CONDITION = "condition"
    API_CALL = "api_call"
    BOOK_APPOINTMENT = "book_appointment"
    QUALIFY_LEAD = "qualify_lead"
    HANDOFF = "handoff"
    TRANSFER = "transfer"
    END = "end"


class _BaseNode(BaseModel):
    id: Annotated[str, Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")]
    label: str | None = None


class MessageNode(_BaseNode):
    """Speak a line, then continue."""

    type: Literal[NodeType.MESSAGE] = NodeType.MESSAGE
    text: Annotated[str, Field(min_length=1, max_length=2000)]
    next: str | None = None


class CollectNode(_BaseNode):
    """Ask a question and store the caller's answer in a flow variable."""

    type: Literal[NodeType.COLLECT] = NodeType.COLLECT
    prompt: Annotated[str, Field(min_length=1, max_length=2000)]
    variable: Annotated[str, Field(min_length=1, max_length=60, pattern=r"^[a-z][a-z0-9_]*$")]
    #: One of: text, number, phone, date, time, email, yes_no
    expects: str = "text"
    max_retries: Annotated[int, Field(ge=0, le=5)] = 2
    next: str | None = None


class IntentBranchNode(_BaseNode):
    """Route on the intent classified from the caller's last utterance."""

    type: Literal[NodeType.INTENT_BRANCH] = NodeType.INTENT_BRANCH
    branches: dict[str, str] = Field(default_factory=dict)
    default: str | None = None

    @model_validator(mode="after")
    def _needs_a_route(self) -> IntentBranchNode:
        if not self.branches and not self.default:
            raise ValueError("intent_branch needs at least one branch or a default.")
        return self


class ConditionNode(_BaseNode):
    """Branch on a previously collected variable."""

    type: Literal[NodeType.CONDITION] = NodeType.CONDITION
    variable: Annotated[str, Field(min_length=1, max_length=60)]
    #: One of: eq, neq, gt, gte, lt, lte, contains, exists
    operator: str = "eq"
    value: Any = None
    if_true: str | None = None
    if_false: str | None = None


class ApiCallNode(_BaseNode):
    """Call a business system (CRM, calendar) mid-conversation."""

    type: Literal[NodeType.API_CALL] = NodeType.API_CALL
    url: Annotated[str, Field(min_length=1, max_length=500)]
    method: Literal["GET", "POST", "PUT", "PATCH"] = "POST"
    headers: dict[str, str] = Field(default_factory=dict)
    body_template: dict[str, Any] = Field(default_factory=dict)
    #: Where to store the response, e.g. ``{"slot_id": "$.data.id"}``.
    save_as: dict[str, str] = Field(default_factory=dict)
    timeout_seconds: Annotated[float, Field(gt=0, le=15)] = 5.0
    next: str | None = None
    on_error: str | None = None

    @model_validator(mode="after")
    def _https_only(self) -> ApiCallNode:
        if not self.url.startswith(("http://", "https://")):
            raise ValueError("api_call url must be an absolute http(s) URL.")
        return self


class BookAppointmentNode(_BaseNode):
    """Book the slot the caller asked for, in this tenant's calendar.

    The variables named here are filled by earlier ``collect`` nodes. The time
    variable holds the caller's own words ("kal subah gyarah baje"); the engine
    resolves them against the business timezone.
    """

    type: Literal[NodeType.BOOK_APPOINTMENT] = NodeType.BOOK_APPOINTMENT
    name_variable: Annotated[str, Field(max_length=60)] = "customer_name"
    time_variable: Annotated[str, Field(max_length=60)] = "preferred_time"
    service_variable: Annotated[str | None, Field(default=None, max_length=60)] = None
    #: Defaults to the tenant's configured slot length.
    duration_minutes: Annotated[int | None, Field(default=None, ge=5, le=480)] = None
    #: How many alternatives to offer when the requested slot is gone.
    offer_alternatives: Annotated[int, Field(ge=0, le=5)] = 2
    next: str | None = None
    #: Taken slot, or a time outside opening hours.
    on_unavailable: str | None = None
    #: Nothing bookable could be understood from the caller's words.
    on_error: str | None = None


class QualifyLeadNode(_BaseNode):
    """Score the caller against BANT and route on the result (design doc §4.9).

    The four answers come from earlier ``collect`` nodes; this node only reads
    them, scores the lead and writes it to the pipeline. Routing is by tier so
    a flow author can hand a hot lead straight to a salesperson while a cold
    one gets a polite close.
    """

    type: Literal[NodeType.QUALIFY_LEAD] = NodeType.QUALIFY_LEAD
    name_variable: Annotated[str, Field(max_length=60)] = "contact_name"
    budget_variable: Annotated[str, Field(max_length=60)] = "budget"
    authority_variable: Annotated[str, Field(max_length=60)] = "authority"
    need_variable: Annotated[str, Field(max_length=60)] = "need"
    timeline_variable: Annotated[str, Field(max_length=60)] = "timeline"
    company_variable: Annotated[str | None, Field(default=None, max_length=60)] = None
    interest_variable: Annotated[str | None, Field(default=None, max_length=60)] = None
    #: Spoken when the lead qualifies. Left unset, the node says nothing and
    #: the next node does the talking.
    qualified_message: Annotated[str | None, Field(default=None, max_length=2000)] = None
    unqualified_message: Annotated[str | None, Field(default=None, max_length=2000)] = None
    next: str | None = None
    #: Taken when the lead clears the tenant's ``hot_at`` threshold.
    on_hot: str | None = None
    #: Taken when the lead scores below the tenant's ``qualify_at`` threshold.
    on_unqualified: str | None = None


class HandoffNode(_BaseNode):
    """Escalate off the voice channel."""

    type: Literal[NodeType.HANDOFF] = NodeType.HANDOFF
    channel: Literal["whatsapp", "sms", "email"] = "whatsapp"
    message: str | None = None
    next: str | None = None


class TransferNode(_BaseNode):
    """Bridge the call to a human."""

    type: Literal[NodeType.TRANSFER] = NodeType.TRANSFER
    to_number: str | None = None
    announcement: str | None = None


class EndNode(_BaseNode):
    type: Literal[NodeType.END] = NodeType.END
    text: str | None = None
    disposition: Literal["resolved", "unresolved", "escalated", "handed_off"] = "resolved"


FlowNode = Annotated[
    MessageNode
    | CollectNode
    | IntentBranchNode
    | ConditionNode
    | ApiCallNode
    | BookAppointmentNode
    | QualifyLeadNode
    | HandoffNode
    | TransferNode
    | EndNode,
    Field(discriminator="type"),
]


class ConversationFlow(BaseModel):
    """A validated flow graph."""

    start_node: Annotated[str, Field(min_length=1, max_length=80)]
    nodes: Annotated[list[FlowNode], Field(min_length=1, max_length=MAX_NODES)]
    variables: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_graph(self) -> ConversationFlow:
        ids = [node.id for node in self.nodes]
        duplicates = {i for i in ids if ids.count(i) > 1}
        if duplicates:
            raise ValueError(f"Duplicate node ids: {', '.join(sorted(duplicates))}")
        known = set(ids)
        if self.start_node not in known:
            raise ValueError(f"start_node '{self.start_node}' is not a defined node.")

        dangling: list[str] = [
            f"{node.id} -> {target}"
            for node in self.nodes
            for target in outgoing_edges(node)
            if target not in known
        ]
        if dangling:
            raise ValueError(f"Edges point at unknown nodes: {', '.join(sorted(dangling))}")

        unreachable = known - _reachable_from(self.start_node, self.node_map)
        if unreachable:
            raise ValueError(f"Unreachable nodes: {', '.join(sorted(unreachable))}")
        return self

    @property
    def node_map(self) -> dict[str, Any]:
        return {node.id: node for node in self.nodes}

    def get_node(self, node_id: str):
        return self.node_map.get(node_id)

    def terminal_node_ids(self) -> set[str]:
        return {n.id for n in self.nodes if n.type in (NodeType.END, NodeType.TRANSFER)}


def outgoing_edges(node) -> list[str]:
    """All node ids this node can move to."""
    edges: list[str] = []
    for attr in (
        "next",
        "default",
        "if_true",
        "if_false",
        "on_error",
        "on_unavailable",
        "on_hot",
        "on_unqualified",
    ):
        target = getattr(node, attr, None)
        if target:
            edges.append(target)
    branches = getattr(node, "branches", None)
    if branches:
        edges.extend(branches.values())
    return edges


def _reachable_from(start: str, node_map: dict[str, Any]) -> set[str]:
    seen: set[str] = set()
    stack = [start]
    while stack:
        current = stack.pop()
        if current in seen or current not in node_map:
            continue
        seen.add(current)
        stack.extend(outgoing_edges(node_map[current]))
    return seen


def validate_flow(flow_json: dict | None) -> ConversationFlow | None:
    """Parse and validate a flow, translating pydantic errors into API errors.

    An empty or absent flow is allowed — a draft agent may have no flow yet, and
    the conversation engine falls back to purely LLM-driven dialogue.
    """
    if not flow_json:
        return None
    try:
        return ConversationFlow.model_validate(flow_json)
    except PydanticValidationError as exc:
        raise ValidationError(
            "Conversation flow is invalid.",
            details={"errors": [_describe(e) for e in exc.errors()]},
        ) from exc


def _describe(error: dict) -> dict:
    return {
        "location": ".".join(str(part) for part in error.get("loc", ())),
        "message": error.get("msg", "invalid"),
        "type": error.get("type", "value_error"),
    }


def default_flow(greeting: str, use_case: str = "customer_support") -> dict:
    """The starter flow used when an agent is created without one.

    Appointment-booking agents get a flow that actually books, rather than a
    generic shell the owner would have to wire up before the agent is useful.
    """
    if use_case == "appointment_booking":
        return appointment_booking_flow(greeting)
    if use_case == "lead_qualification":
        return lead_qualification_flow(greeting)
    return {
        "start_node": "greeting",
        "nodes": [
            {"id": "greeting", "type": "message", "text": greeting, "next": "listen"},
            {
                "id": "listen",
                "type": "collect",
                "prompt": "How may I help you today?",
                "variable": "caller_request",
                "next": "route",
            },
            {
                "id": "route",
                "type": "intent_branch",
                "branches": {},
                "default": "wrap_up",
            },
            {
                "id": "wrap_up",
                "type": "end",
                "text": "Thank you for calling. Have a good day!",
                "disposition": "resolved",
            },
        ],
        "variables": {"use_case": use_case},
    }


def appointment_booking_flow(greeting: str) -> dict:
    """A working booking flow: name, preferred time, book, confirm.

    The ``book_appointment`` node is what makes this more than a script — it
    writes into the tenant's calendar and reroutes to ``retry_time`` when the
    requested slot is gone, having already spoken the nearest alternatives.
    """
    return {
        "start_node": "greeting",
        "nodes": [
            {"id": "greeting", "type": "message", "text": greeting, "next": "ask_name"},
            {
                "id": "ask_name",
                "type": "collect",
                "prompt": "May I have your name, please?",
                "variable": "customer_name",
                "next": "ask_time",
            },
            {
                "id": "ask_time",
                "type": "collect",
                "prompt": "Which day and time would suit you?",
                "variable": "preferred_time",
                "expects": "date",
                "next": "book",
            },
            {
                "id": "book",
                "type": "book_appointment",
                "name_variable": "customer_name",
                "time_variable": "preferred_time",
                "offer_alternatives": 2,
                "next": "wrap_up",
                "on_unavailable": "retry_time",
                "on_error": "retry_time",
            },
            {
                "id": "retry_time",
                "type": "collect",
                "prompt": "Which of those times works for you?",
                "variable": "preferred_time",
                "expects": "date",
                "next": "book_retry",
            },
            {
                "id": "book_retry",
                "type": "book_appointment",
                "name_variable": "customer_name",
                "time_variable": "preferred_time",
                # One retry only: a second failure hands off rather than looping.
                "offer_alternatives": 0,
                "next": "wrap_up",
                "on_unavailable": "handoff",
                "on_error": "handoff",
            },
            {
                "id": "handoff",
                "type": "handoff",
                "channel": "whatsapp",
                "message": "I am sending you our available times on WhatsApp.",
            },
            {
                "id": "wrap_up",
                "type": "end",
                "text": "Thank you for calling. See you then!",
                "disposition": "resolved",
            },
        ],
        "variables": {"use_case": "appointment_booking"},
    }


def lead_qualification_flow(greeting: str) -> dict:
    """A working BANT flow: ask the four questions, score, then route on tier.

    The ``qualify_lead`` node reads the answers the ``collect`` nodes gathered
    and writes a scored lead into the pipeline, so a hot prospect is transferred
    to a salesperson while the call is still live rather than waiting in a queue
    for someone to notice the transcript.
    """
    return {
        "start_node": "greeting",
        "nodes": [
            {"id": "greeting", "type": "message", "text": greeting, "next": "ask_name"},
            {
                "id": "ask_name",
                "type": "collect",
                "prompt": "May I have your name, please?",
                "variable": "contact_name",
                "next": "ask_need",
            },
            {
                "id": "ask_need",
                "type": "collect",
                "prompt": "What are you looking to solve?",
                "variable": "need",
                "next": "ask_timeline",
            },
            {
                "id": "ask_timeline",
                "type": "collect",
                "prompt": "When are you hoping to get started?",
                "variable": "timeline",
                "next": "ask_budget",
            },
            {
                "id": "ask_budget",
                "type": "collect",
                "prompt": "Do you have a budget in mind for this?",
                "variable": "budget",
                "next": "ask_authority",
            },
            {
                "id": "ask_authority",
                "type": "collect",
                "prompt": "And who else is involved in the decision?",
                "variable": "authority",
                "next": "qualify",
            },
            {
                "id": "qualify",
                "type": "qualify_lead",
                "next": "wrap_up",
                # A hot lead gets a firmer promise than everyone else. The
                # owner swaps this for a ``transfer`` node once they have a
                # sales number to dial — a starter flow cannot invent one, and
                # a transfer to an empty number would drop the best caller of
                # the day.
                "on_hot": "sales_priority",
                "on_unqualified": "wrap_up",
                "unqualified_message": "Thank you — I have noted your details.",
            },
            {
                "id": "sales_priority",
                "type": "message",
                "text": "Our sales team will call you back within the hour.",
                "next": "wrap_up",
            },
            {
                "id": "wrap_up",
                "type": "end",
                "text": "Thank you for your time. Someone will be in touch shortly.",
                "disposition": "resolved",
            },
        ],
        "variables": {"use_case": "lead_qualification"},
    }
