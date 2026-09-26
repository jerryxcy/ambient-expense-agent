"""Unit tests for the ambient expense approval agent."""

import base64
import json
import pytest
from google.adk.events.request_input import RequestInput
from google.adk.events.event import Event
from google.genai import types

from expense_agent.agent import (
    _extract_expense_dict,
    _parse_human_decision,
    auto_approve,
    parse_expense,
    route_expense,
    root_agent,
)
from expense_agent.models import Expense, ExpenseOutcome, ExpenseReview, RiskAssessment
from expense_agent import config


def test_extract_expense_dict_plain_dict():
    payload = {
        "amount": 45.0,
        "submitter": "bob@example.com",
        "category": "Meals",
        "description": "Lunch meeting",
        "date": "2026-09-26",
    }
    extracted = _extract_expense_dict(payload)
    assert extracted["amount"] == 45.0
    assert extracted["submitter"] == "bob@example.com"


def test_extract_expense_dict_data_key():
    payload = {
        "data": {
            "amount": 80.0,
            "submitter": "alice@example.com",
            "category": "Supplies",
            "description": "Notebooks",
            "date": "2026-09-26",
        }
    }
    extracted = _extract_expense_dict(payload)
    assert extracted["amount"] == 80.0
    assert extracted["category"] == "Supplies"


def test_extract_expense_dict_base64_pubsub():
    inner_data = {
        "amount": 250.0,
        "submitter": "carol@example.com",
        "category": "Travel",
        "description": "Flight to conference",
        "date": "2026-09-26",
    }
    b64_str = base64.b64encode(json.dumps(inner_data).encode("utf-8")).decode("utf-8")
    payload = {"message": {"data": b64_str, "messageId": "msg-123"}}

    extracted = _extract_expense_dict(payload)
    assert extracted["amount"] == 250.0
    assert extracted["submitter"] == "carol@example.com"


def test_extract_expense_dict_content_object():
    inner_data = {
        "data": {
            "amount": 12.50,
            "submitter": "dave@example.com",
            "category": "Coffee",
            "description": "Espresso",
            "date": "2026-09-26",
        }
    }
    content = types.Content(
        role="user",
        parts=[types.Part.from_text(text=json.dumps(inner_data))],
    )
    extracted = _extract_expense_dict(content)
    assert extracted["amount"] == 12.50


def test_route_expense_under_threshold():
    expense = Expense(
        amount=99.99,
        submitter="alice@example.com",
        category="Meals",
        description="Team lunch",
        date="2026-09-26",
    )
    event = route_expense(expense)
    assert isinstance(event, Event)
    assert event.route == "auto_approve"


def test_route_expense_at_or_above_threshold():
    expense_at = Expense(
        amount=100.0,
        submitter="bob@example.com",
        category="Travel",
        description="Train ticket",
        date="2026-09-26",
    )
    event_at = route_expense(expense_at)
    assert isinstance(event_at, Event)
    assert event_at.route == "requires_review"

    expense_over = Expense(
        amount=500.0,
        submitter="charlie@example.com",
        category="Software",
        description="Annual license",
        date="2026-09-26",
    )
    event_over = route_expense(expense_over)
    assert isinstance(event_over, Event)
    assert event_over.route == "requires_review"


def test_auto_approve_node():
    expense = Expense(
        amount=42.0,
        submitter="alice@example.com",
        category="Books",
        description="Technical manual",
        date="2026-09-26",
    )
    outcome = auto_approve(expense)
    assert isinstance(outcome, ExpenseOutcome)
    assert outcome.status == "APPROVED"
    assert outcome.reviewed_by == "auto_approval_rule"
    assert outcome.risk_alert is None


def test_parse_human_decision_helpers():
    status, notes = _parse_human_decision("approved, looks reasonable")
    assert status == "APPROVED"

    status, notes = _parse_human_decision({"decision": "REJECT", "notes": "No receipt provided"})
    assert status == "REJECTED"
    assert notes == "No receipt provided"

    status, notes = _parse_human_decision("Rejected: over limit")
    assert status == "REJECTED"


def test_workflow_graph_structure():
    """Validates that the Workflow compiles its graph with expected nodes and terminal outcome."""
    assert root_agent.graph is not None
    node_names = {node.name for node in root_agent.graph.nodes}
    expected_nodes = {
        "START",
        "parse_expense",
        "route_expense",
        "auto_approve",
        "review_risk",
        "human_approval",
        "record_outcome",
    }
    assert expected_nodes.issubset(node_names)
    assert "record_outcome" in root_agent.graph._terminal_node_names
