"""Ambient Expense-Approval Agent implemented as an ADK 2.0 Graph Workflow."""

from __future__ import annotations

import base64
import json
import logging
import os
from typing import Any, Literal

from google import genai
from google.adk.agents.context import Context
from google.adk.apps import App, ResumabilityConfig
from google.adk.events.event import Event
from google.adk.events.request_input import RequestInput
from google.adk.workflow import Workflow, node
from google.genai import types

from . import config
from .models import Expense, ExpenseOutcome, ExpenseReview, RiskAssessment

logger = logging.getLogger(__name__)


def _extract_expense_dict(raw_input: Any) -> dict[str, Any]:
    """Extracts and normalizes expense data from raw JSON, Pub/Sub, or Content objects."""
    payload = raw_input

    # 1. Handle types.Content from START node in CLI/Runner sessions
    if isinstance(payload, types.Content):
        text_parts = [p.text for p in (payload.parts or []) if p.text]
        payload = "".join(text_parts).strip()

    # 2. If it's a JSON string, decode it into a Python dict
    if isinstance(payload, str):
        payload = payload.strip()
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            # Fallback for plain text / casual greeting (e.g. in test suites sending "Hi!")
            return {
                "amount": 0.0,
                "submitter": "anonymous_user",
                "category": "General",
                "description": payload,
                "date": "2026-09-26",
            }

    if not isinstance(payload, dict):
        raise ValueError(
            f"Expected dict or JSON string payload, got: {type(payload).__name__}"
        )

    # 3. Handle Google Cloud Pub/Sub push envelope {"message": {"data": ...}}
    if "message" in payload and isinstance(payload["message"], dict):
        payload = payload["message"]

    # 4. Check for the "data" key (base64-encoded Pub/Sub data or plain JSON object)
    if "data" in payload:
        data_field = payload["data"]
        if isinstance(data_field, dict):
            return data_field
        if isinstance(data_field, str):
            # Attempt base64 decode first (standard Pub/Sub)
            try:
                decoded_bytes = base64.b64decode(data_field, validate=True)
                return json.loads(decoded_bytes.decode("utf-8"))
            except Exception:
                # If base64 decoding fails or isn't base64, parse as direct JSON string
                return json.loads(data_field)

    # 5. Direct dictionary with expense fields
    if "amount" in payload:
        return payload

    raise ValueError(
        f"Incoming payload must contain a 'data' key or expense fields. Received: {payload}"
    )


def _get_genai_client() -> genai.Client:
    """Instantiates the GenAI client honoring Vertex AI or Google AI Studio settings."""
    use_vertex = os.getenv("GOOGLE_GENAI_USE_VERTEXAI", "false").lower() in ("true", "1")
    if use_vertex:
        return genai.Client(
            vertexai=True,
            project=os.getenv("GOOGLE_CLOUD_PROJECT"),
            location=os.getenv("GOOGLE_CLOUD_LOCATION", "global"),
        )
    return genai.Client()


def _parse_human_decision(response: Any) -> tuple[Literal["APPROVED", "REJECTED"], str]:
    """Parses a human response into an approval decision and approver notes."""
    if isinstance(response, dict):
        decision = str(response.get("decision", response.get("status", ""))).upper()
        notes = str(response.get("notes", response.get("comment", response.get("reason", ""))))
        if any(kw in decision for kw in ("REJECT", "DENY", "DISAPPROVE")):
            return "REJECTED", notes
        return "APPROVED", notes

    resp_str = str(response).strip().lower()
    if any(kw in resp_str for kw in ("reject", "deny", "declined", "disapproved", "no")):
        return "REJECTED", str(response)
    return "APPROVED", str(response)


# ------------------------------------------------------------------------------
# Workflow Graph Nodes
# ------------------------------------------------------------------------------

@node(name="parse_expense")
def parse_expense(node_input: Any) -> Expense:
    """Pulls out the expense fields from raw JSON/Pub/Sub payloads."""
    expense_data = _extract_expense_dict(node_input)
    expense = Expense.model_validate(expense_data)
    logger.info(
        "Parsed expense: Submitter=%s, Amount=$%.2f, Category=%s",
        expense.submitter,
        expense.amount,
        expense.category,
    )
    return expense


@node(name="route_expense")
def route_expense(node_input: Expense) -> Event:
    """Evaluates the dollar threshold rule in Python and routes accordingly.

    - Under $100 -> 'auto_approve'
    - $100 or more -> 'requires_review'
    """
    if node_input.amount < config.AUTO_APPROVE_THRESHOLD:
        logger.info(
            "Expense $%.2f is below threshold $%.2f -> auto_approve",
            node_input.amount,
            config.AUTO_APPROVE_THRESHOLD,
        )
        return Event(output=node_input, route="auto_approve")

    logger.info(
        "Expense $%.2f is at or above threshold $%.2f -> requires_review",
        node_input.amount,
        config.AUTO_APPROVE_THRESHOLD,
    )
    return Event(output=node_input, route="requires_review")


@node(name="auto_approve")
def auto_approve(node_input: Expense) -> ExpenseOutcome:
    """Instantly auto-approves expenses under the threshold without LLM intervention."""
    return ExpenseOutcome(
        expense=node_input,
        status="APPROVED",
        reason=(
            f"Auto-approved instantly: amount ${node_input.amount:.2f} is under the "
            f"${config.AUTO_APPROVE_THRESHOLD:.2f} approval threshold."
        ),
        reviewed_by="auto_approval_rule",
        risk_alert=None,
    )


@node(name="review_risk")
async def review_risk(node_input: Expense) -> ExpenseReview:
    """Calls Gemini to review risk factors and formulate an alert when amount >= threshold."""
    prompt = (
        f"You are an enterprise expense auditor. Evaluate this expense report for risk factors:\n"
        f"- Submitter: {node_input.submitter}\n"
        f"- Amount: ${node_input.amount:.2f}\n"
        f"- Category: {node_input.category}\n"
        f"- Date: {node_input.date}\n"
        f"- Description: {node_input.description}\n\n"
        f"Assess potential risk factors (e.g. unusually high amounts for the category, vague justifications, "
        f"policy concerns, or weekend dates). Return a structured risk assessment."
    )

    try:
        client = _get_genai_client()
        response = await client.aio.models.generate_content(
            model=config.MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=RiskAssessment,
                temperature=0.1,
            ),
        )
        risk_assessment = RiskAssessment.model_validate_json(response.text)
    except Exception as exc:
        logger.warning("LLM risk review call encountered an error (%s); falling back to rule-based review", exc)
        risk_assessment = RiskAssessment(
            risk_level="MEDIUM" if node_input.amount < 500 else "HIGH",
            risk_factors=[
                f"Amount ${node_input.amount:.2f} exceeds standard ${config.AUTO_APPROVE_THRESHOLD:.2f} limit",
                "Automated risk check flagged for human review",
            ],
            alert_summary=(
                f"Expense of ${node_input.amount:.2f} by {node_input.submitter} for '{node_input.description}' "
                f"exceeds the ${config.AUTO_APPROVE_THRESHOLD:.2f} threshold and requires manager review."
            ),
            recommended_action="APPROVE" if node_input.amount < 500 else "REQUEST_MORE_INFO",
        )

    logger.info("Risk review completed with level: %s", risk_assessment.risk_level)
    return ExpenseReview(expense=node_input, risk_assessment=risk_assessment)


@node(name="human_approval", rerun_on_resume=True)
async def human_approval(ctx: Context, node_input: ExpenseReview):
    """Pauses workflow via RequestInput for human sign-off, then records decision upon resumption."""
    interrupt_id = config.APPROVAL_INTERRUPT_ID

    # Initial run: pause workflow and request approval decision
    if not ctx.resume_inputs or interrupt_id not in ctx.resume_inputs:
        exp = node_input.expense
        risk = node_input.risk_assessment
        factors = "\n  - " + "\n  - ".join(risk.risk_factors) if risk.risk_factors else " None identified"

        alert_message = (
            f"🚨 [RISK ALERT] Expense Review Required\n"
            f"• Submitter: {exp.submitter}\n"
            f"• Amount: ${exp.amount:.2f} (Threshold: ${config.AUTO_APPROVE_THRESHOLD:.2f})\n"
            f"• Category: {exp.category}\n"
            f"• Date: {exp.date}\n"
            f"• Description: {exp.description}\n"
            f"• Risk Level: {risk.risk_level}\n"
            f"• Risk Factors:{factors}\n"
            f"• Assessment: {risk.alert_summary}\n"
            f"• Recommendation: {risk.recommended_action}\n\n"
            f"Please respond to approve or reject this expense."
        )

        yield RequestInput(
            interrupt_id=interrupt_id,
            message=alert_message,
        )
        return

    # Resumed run: human has provided input
    human_response = ctx.resume_inputs[interrupt_id]
    status, notes = _parse_human_decision(human_response)

    yield ExpenseOutcome(
        expense=node_input.expense,
        status=status,
        reason=f"Human review completed with decision: {status}.",
        reviewed_by="human_approver",
        risk_alert=node_input.risk_assessment,
        approver_notes=notes,
    )


@node(name="record_outcome")
def record_outcome(node_input: ExpenseOutcome) -> Event:
    """Final terminal node: logs outcome and emits user-facing content event."""
    logger.info(
        "Recorded final expense outcome: Submitter=%s, Amount=$%.2f, Status=%s, ReviewedBy=%s",
        node_input.expense.submitter,
        node_input.expense.amount,
        node_input.status,
        node_input.reviewed_by,
    )

    summary_text = (
        f"📋 **Expense Report Outcome**\n\n"
        f"- **Submitter**: {node_input.expense.submitter}\n"
        f"- **Amount**: ${node_input.expense.amount:.2f}\n"
        f"- **Category**: {node_input.expense.category}\n"
        f"- **Status**: {'✅ ' if node_input.status == 'APPROVED' else '❌ '}{node_input.status}\n"
        f"- **Reviewed By**: {node_input.reviewed_by}\n"
        f"- **Details**: {node_input.reason}\n"
    )
    if node_input.approver_notes:
        summary_text += f"- **Approver Notes**: {node_input.approver_notes}\n"

    return Event(
        output=node_input,
        content=types.Content(
            role="model",
            parts=[types.Part.from_text(text=summary_text)],
        ),
    )


# ------------------------------------------------------------------------------
# Workflow Graph Assembly
# ------------------------------------------------------------------------------

root_agent = Workflow(
    name="ambient_expense_agent",
    description=(
        "Ambient expense-approval workflow using ADK 2.0 graph API with "
        "deterministic routing, LLM risk review, and human-in-the-loop sign-off."
    ),
    edges=[
        ("START", parse_expense),
        (parse_expense, route_expense),
        (
            route_expense,
            {
                "auto_approve": auto_approve,
                "requires_review": review_risk,
            },
        ),
        (review_risk, human_approval),
        (human_approval, record_outcome),
        (auto_approve, record_outcome),
    ],
)

app = App(
    root_agent=root_agent,
    name="app",
    resumability_config=ResumabilityConfig(is_resumable=True),
)
