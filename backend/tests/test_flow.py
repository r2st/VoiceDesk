"""Conversation flow graph validation (design doc §4.1)."""

from __future__ import annotations

import pytest

from app.core.errors import ValidationError
from app.services.flow import (
    ConversationFlow,
    NodeType,
    default_flow,
    outgoing_edges,
    validate_flow,
)


def flow(*nodes, start: str = "a") -> dict:
    return {"start_node": start, "nodes": list(nodes)}


MESSAGE = {"id": "a", "type": "message", "text": "Hello", "next": "b"}
END = {"id": "b", "type": "end", "text": "Bye"}


class TestValidation:
    def test_minimal_valid_flow(self):
        parsed = validate_flow(flow(MESSAGE, END))
        assert isinstance(parsed, ConversationFlow)
        assert parsed.start_node == "a"
        assert len(parsed.nodes) == 2

    def test_empty_flow_is_allowed(self):
        """A draft agent may have no flow; the engine falls back to the LLM."""
        assert validate_flow(None) is None
        assert validate_flow({}) is None

    def test_start_node_must_exist(self):
        with pytest.raises(ValidationError) as exc:
            validate_flow(flow(MESSAGE, END, start="missing"))
        assert any("start_node" in e["message"] for e in exc.value.details["errors"])

    def test_duplicate_node_ids_are_rejected(self):
        with pytest.raises(ValidationError) as exc:
            validate_flow(flow(MESSAGE, {**MESSAGE}, END))
        assert any("Duplicate node ids" in e["message"] for e in exc.value.details["errors"])

    def test_dangling_edge_is_rejected(self):
        with pytest.raises(ValidationError) as exc:
            validate_flow(flow({**MESSAGE, "next": "nowhere"}, END))
        errors = " ".join(e["message"] for e in exc.value.details["errors"])
        assert "unknown nodes" in errors

    def test_unreachable_node_is_rejected(self):
        """An orphan node is almost always an editing mistake, so it fails loudly."""
        orphan = {"id": "orphan", "type": "end", "text": "never"}
        with pytest.raises(ValidationError) as exc:
            validate_flow(flow(MESSAGE, END, orphan))
        errors = " ".join(e["message"] for e in exc.value.details["errors"])
        assert "Unreachable nodes: orphan" in errors

    def test_unknown_node_type_is_rejected(self):
        with pytest.raises(ValidationError):
            validate_flow(flow({"id": "a", "type": "teleport"}))

    def test_at_least_one_node_is_required(self):
        with pytest.raises(ValidationError):
            validate_flow({"start_node": "a", "nodes": []})

    def test_error_details_carry_a_location(self):
        with pytest.raises(ValidationError) as exc:
            validate_flow({"start_node": "a"})
        assert exc.value.details["errors"][0]["location"]


class TestNodeTypes:
    def test_intent_branch_edges_include_every_branch(self):
        parsed = validate_flow(
            flow(
                {
                    "id": "a",
                    "type": "intent_branch",
                    "branches": {"book": "b", "cancel": "c"},
                    "default": "c",
                },
                {"id": "b", "type": "end", "text": "booked"},
                {"id": "c", "type": "end", "text": "bye"},
            )
        )
        assert set(outgoing_edges(parsed.get_node("a"))) == {"b", "c"}

    def test_condition_node_has_both_arms(self):
        parsed = validate_flow(
            flow(
                {
                    "id": "a",
                    "type": "condition",
                    "variable": "amount",
                    "operator": "gt",
                    "value": 100,
                    "if_true": "b",
                    "if_false": "c",
                },
                {"id": "b", "type": "end", "text": "high"},
                {"id": "c", "type": "end", "text": "low"},
            )
        )
        assert set(outgoing_edges(parsed.get_node("a"))) == {"b", "c"}

    def test_api_call_node_error_edge_is_followed(self):
        parsed = validate_flow(
            flow(
                {
                    "id": "a",
                    "type": "api_call",
                    "url": "https://crm.test/lookup",
                    "next": "b",
                    "on_error": "c",
                },
                {"id": "b", "type": "end", "text": "ok"},
                {"id": "c", "type": "end", "text": "failed"},
            )
        )
        assert set(outgoing_edges(parsed.get_node("a"))) == {"b", "c"}

    def test_terminal_nodes_are_end_and_transfer(self):
        parsed = validate_flow(
            flow(
                {"id": "a", "type": "message", "text": "hi", "next": "t"},
                {"id": "t", "type": "transfer", "to_number": "+919000000000"},
            )
        )
        assert parsed.terminal_node_ids() == {"t"}

    def test_handoff_node_is_not_terminal(self):
        """A WhatsApp handoff continues the call; only end/transfer stop it."""
        parsed = validate_flow(
            flow(
                {"id": "a", "type": "handoff", "reason": "caller_request", "next": "b"},
                END,
            )
        )
        assert parsed.terminal_node_ids() == {"b"}


class TestDefaultFlow:
    def test_is_valid(self):
        parsed = validate_flow(default_flow("Namaste!"))
        assert parsed is not None
        assert parsed.start_node == "greeting"

    def test_records_the_use_case(self):
        parsed = validate_flow(default_flow("Hi", "payment_reminder"))
        assert parsed.variables["use_case"] == "payment_reminder"

    def test_greeting_text_is_used(self):
        parsed = validate_flow(default_flow("Custom greeting"))
        assert parsed.get_node("greeting").text == "Custom greeting"

    def test_ends_in_a_terminal_node(self):
        assert validate_flow(default_flow("Hi")).terminal_node_ids() == {"wrap_up"}


class TestFlowEndpoint:
    async def test_validate_endpoint_accepts_a_good_flow(self, client, owner_headers):
        response = await client.post(
            "/api/v1/agents/validate-flow",
            headers=owner_headers,
            json={"flow_json": flow(MESSAGE, END)},
        )
        body = response.json()
        assert body["valid"] is True
        assert body["node_count"] == 2
        assert body["terminal_nodes"] == ["b"]

    async def test_validate_endpoint_reports_errors_without_raising(self, client, owner_headers):
        response = await client.post(
            "/api/v1/agents/validate-flow",
            headers=owner_headers,
            json={"flow_json": flow({**MESSAGE, "next": "nowhere"}, END)},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["valid"] is False and body["errors"]

    async def test_empty_flow_reports_invalid_rather_than_crashing(self, client, owner_headers):
        response = await client.post(
            "/api/v1/agents/validate-flow", headers=owner_headers, json={"flow_json": {}}
        )
        assert response.json()["valid"] is False

    async def test_updating_an_agent_flow_bumps_its_version(self, client, owner_headers, agent):
        before = agent.flow_version
        response = await client.put(
            f"/api/v1/agents/{agent.id}/flow",
            headers=owner_headers,
            json={"flow_json": flow(MESSAGE, END)},
        )
        assert response.status_code == 200
        assert response.json()["flow_version"] == before + 1

    async def test_invalid_flow_is_rejected_on_save(self, client, owner_headers, agent):
        response = await client.put(
            f"/api/v1/agents/{agent.id}/flow",
            headers=owner_headers,
            json={"flow_json": flow({**MESSAGE, "next": "nowhere"}, END)},
        )
        assert response.status_code == 422

    def test_node_type_enum_covers_the_builder_palette(self):
        assert {t.value for t in NodeType} == {
            "message",
            "collect",
            "intent_branch",
            "condition",
            "api_call",
            "book_appointment",
            "handoff",
            "transfer",
            "end",
        }
