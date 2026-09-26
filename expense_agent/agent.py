"""Ambient Expense-Approval Agent implemented as an ADK 2.0 Graph Workflow."""

from __future__ import annotations

import base64
import json
import logging
import os
import re
from typing import Any, Literal, TypeVar

from google import genai
from google.adk.agents.context import Context
from google.adk.apps import App, ResumabilityConfig
from google.adk.events.event import Event
from google.adk.events.request_input import RequestInput
from google.adk.workflow import Workflow, node
from google.genai import types
from pydantic import BaseModel

from . import config
from .models import Expense, ExpenseOutcome, ExpenseReview, RiskAssessment
from .security import detect_prompt_injection, scrub_pii

logger = logging.getLogger(__name__)

ModelT = TypeVar("ModelT", bound=BaseModel)


def _extract_expense_dict(raw_input: Any) -> dict[str, Any]:
    """Extracts and normalizes expense data from raw JSON, Pub/Sub, or Content objects."""
    if raw_input is None:
        return {
            "amount": 0.0,
            "submitter": "anonymous_user",
            "category": "General",
            "description": "None",
            "date": "2026-09-26",
        }

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


_NEGATED_APPROVAL = re.compile(
    r"\b(not|don'?t|never|cannot|can'?t|won'?t)\s+(be\s+)?(approv|ok\b|okay\b|good\b|fine\b)", re.IGNORECASE
)
_REJECT_WORDS = re.compile(r"\b(reject(ed)?|den(y|ied)|declin(e|ed)|disapprov(e|ed))\b", re.IGNORECASE)
_LEADING_NO = re.compile(r"^\s*(no|nope)\b", re.IGNORECASE)
# "approval" is deliberately excluded: "needs approval" is not an approval.
_APPROVE_WORDS = re.compile(r"\b(approve[ds]?|yes|lgtm|ok(ay)?)\b", re.IGNORECASE)


def _classify_decision(text: str) -> Literal["APPROVED", "REJECTED"] | None:
    """Classifies free text as an approval or rejection; None when it is ambiguous."""
    if _NEGATED_APPROVAL.search(text) or _REJECT_WORDS.search(text) or _LEADING_NO.search(text):
        return "REJECTED"
    if _APPROVE_WORDS.search(text):
        return "APPROVED"
    return None


def _parse_human_decision(response: Any) -> tuple[Literal["APPROVED", "REJECTED"], str]:
    """Parses a human response into an approval decision and approver notes.

    Fails closed: a response with no clear approval is treated as a rejection.
    """
    if isinstance(response, dict):
        decision_text = str(response.get("decision", response.get("status", "")))
        notes = str(response.get("notes", response.get("comment", response.get("reason", ""))))
    else:
        decision_text = str(response).strip()
        notes = decision_text

    decision = _classify_decision(decision_text)
    if decision is None:
        logger.warning("Ambiguous approver response %r; rejecting by default", decision_text)
        return "REJECTED", f"[Ambiguous response, rejected by default] {notes}".strip()
    return decision, notes


def _resolve_input(node_input: Any, ctx: Context | None, model: type[ModelT], *state_keys: str) -> ModelT:
    """Coerces node input into `model`, falling back to ctx.state on resume/replay.

    Raises instead of fabricating a default, so missing data can never turn into an approval.
    """
    if isinstance(node_input, model):
        return node_input
    if isinstance(node_input, dict):
        return model.model_validate(node_input)
    if ctx is not None:
        for key in state_keys:
            if key in ctx.state:
                return model.model_validate(ctx.state[key])
    raise ValueError(
        f"No {model.__name__} available from node input or session state keys {state_keys}"
    )


def _scrub_expense(expense: Expense) -> Expense:
    """Returns a copy of the expense with PII removed from its description."""
    sanitized_description, categories = scrub_pii(expense.description)
    merged = list(dict.fromkeys([*expense.redacted_categories, *categories]))
    return expense.model_copy(
        update={"description": sanitized_description, "redacted_categories": merged}
    )


# ------------------------------------------------------------------------------
# Workflow Graph Functions (Auto-wrapped by ADK into FunctionNodes)
# ------------------------------------------------------------------------------

def parse_expense(node_input: Any, ctx: Context | None = None) -> Event:
    """Pulls out expense fields, handles replay/resume, and caches to ctx.state.

    PII is scrubbed here, before anything is written to session state or logs.
    """
    if node_input is None and ctx and "expense" in ctx.state:
        expense = Expense.model_validate(ctx.state["expense"])
        return Event(output=expense)

    expense_data = _extract_expense_dict(node_input)
    expense = _scrub_expense(Expense.model_validate(expense_data))
    logger.info(
        "Parsed expense: Submitter=%s, Amount=$%.2f, Category=%s",
        expense.submitter,
        expense.amount,
        expense.category,
    )
    state = {"expense": expense.model_dump()} if ctx else None
    return Event(output=expense, state=state)


def route_expense(node_input: Any, ctx: Context | None = None) -> Event:
    """Evaluates the dollar threshold rule in Python and routes accordingly.

    - Under $100 -> 'auto_approve'
    - $100 or more -> 'requires_review'
    """
    expense = _resolve_input(node_input, ctx, Expense, "expense")

    if expense.amount < config.AUTO_APPROVE_THRESHOLD:
        logger.info(
            "Expense $%.2f is below threshold $%.2f -> auto_approve",
            expense.amount,
            config.AUTO_APPROVE_THRESHOLD,
        )
        return Event(output=expense, route="auto_approve")

    logger.info(
        "Expense $%.2f is at or above threshold $%.2f -> requires_review",
        expense.amount,
        config.AUTO_APPROVE_THRESHOLD,
    )
    return Event(output=expense, route="requires_review")


def auto_approve(node_input: Any, ctx: Context | None = None) -> Event:
    """Instantly auto-approves expenses under the threshold without LLM intervention."""
    expense = _resolve_input(node_input, ctx, Expense, "expense")

    outcome = ExpenseOutcome(
        expense=expense,
        status="APPROVED",
        reason=(
            f"Auto-approved instantly: amount ${expense.amount:.2f} is under the "
            f"${config.AUTO_APPROVE_THRESHOLD:.2f} approval threshold."
        ),
        reviewed_by="auto_approval_rule",
        risk_alert=None,
    )
    state = {"outcome": outcome.model_dump()} if ctx else None
    return Event(output=outcome, state=state)


def security_checkpoint(node_input: Any, ctx: Context | None = None) -> Event:
    """Security Checkpoint before the LLM reviewer.

    1. Scrubs personal data (SSNs and credit cards) from description to ensure
       PII never reaches the model, logs, or downstream payloads.
    2. Detects prompt injection attempts aiming to force auto-approval or bypass rules.
       If detected, bypasses the LLM reviewer and routes straight to human approval.
    """
    expense = _resolve_input(node_input, ctx, Expense, "expense")

    # 1. Scrub PII from description (idempotent if parse_expense already did it)
    sanitized_expense = _scrub_expense(expense)

    if sanitized_expense.redacted_categories:
        logger.info(
            "PII redacted for submitter %s: %s",
            expense.submitter,
            ", ".join(sanitized_expense.redacted_categories),
        )

    # 2. Defend against prompt injection in every free-text field that reaches the LLM prompt
    matched_patterns: list[str] = []
    for text in (expense.submitter, expense.category, expense.date, expense.description):
        _, field_patterns = detect_prompt_injection(text)
        matched_patterns.extend(p for p in field_patterns if p not in matched_patterns)
    is_injection = bool(matched_patterns)

    if is_injection:
        logger.warning(
            "SECURITY EVENT DETECTED: Prompt injection attempt from submitter %s (patterns: %s). Bypassing LLM.",
            expense.submitter,
            ", ".join(matched_patterns),
        )
        flagged_expense = sanitized_expense.model_copy(update={"is_security_event": True})
        security_assessment = RiskAssessment(
            risk_level="HIGH",
            risk_factors=[
                "PROMPT_INJECTION_DETECTED",
                f"Suspicious instruction patterns: {', '.join(matched_patterns)}",
            ],
            alert_summary=(
                "SECURITY EVENT: The expense payload contains adversarial instructions attempting to "
                "force auto-approval or bypass approval rules. LLM review was bypassed to prevent model manipulation."
            ),
            recommended_action="REJECT",
            is_security_event=True,
        )
        security_review = ExpenseReview(
            expense=flagged_expense,
            risk_assessment=security_assessment,
        )
        # Route straight to human approval, model never sees it
        return Event(
            output=security_review,
            route="security_alert",
            state={"review": security_review.model_dump(), "sanitized_expense": flagged_expense.model_dump()},
        )

    # Clean expense continues on to the LLM reviewer
    logger.info("Security checkpoint passed cleanly for expense $%.2f", sanitized_expense.amount)
    return Event(
        output=sanitized_expense,
        route="clean",
        state={"sanitized_expense": sanitized_expense.model_dump()},
    )


async def review_risk(node_input: Any, ctx: Context | None = None) -> Event:
    """Calls Gemini to review risk factors and formulate an alert when amount >= threshold.

    Receives the sanitized expense from security_checkpoint.
    """
    expense = _resolve_input(node_input, ctx, Expense, "sanitized_expense", "expense")

    prompt = (
        f"You are an enterprise expense auditor. Evaluate this expense report for risk factors:\n"
        f"- Submitter: {expense.submitter}\n"
        f"- Amount: ${expense.amount:.2f}\n"
        f"- Category: {expense.category}\n"
        f"- Date: {expense.date}\n"
        f"- Description: {expense.description}\n\n"
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
        logger.warning(
            "LLM risk review call encountered an error (%s); falling back to rule-based review",
            exc,
        )
        risk_assessment = RiskAssessment(
            risk_level="MEDIUM" if expense.amount < 500 else "HIGH",
            risk_factors=[
                f"Amount ${expense.amount:.2f} exceeds standard ${config.AUTO_APPROVE_THRESHOLD:.2f} limit",
                "Automated risk check flagged for human review",
            ],
            alert_summary=(
                f"Expense of ${expense.amount:.2f} by {expense.submitter} for '{expense.description}' "
                f"exceeds the ${config.AUTO_APPROVE_THRESHOLD:.2f} threshold and requires manager review."
            ),
            recommended_action="APPROVE" if expense.amount < 500 else "REQUEST_MORE_INFO",
        )

    logger.info("Risk review completed with level: %s", risk_assessment.risk_level)
    review = ExpenseReview(expense=expense, risk_assessment=risk_assessment)
    return Event(output=review, state={"review": review.model_dump()})


async def human_approval(ctx: Context, node_input: Any):
    """Pauses workflow via RequestInput for human sign-off, then records decision upon resumption.

    Receives ExpenseReview either from review_risk (clean path) or from security_checkpoint (security_alert path).
    """
    review = _resolve_input(node_input, ctx, ExpenseReview, "review")

    interrupt_id = config.APPROVAL_INTERRUPT_ID

    # Initial run: pause workflow and request approval decision
    if not ctx.resume_inputs or interrupt_id not in ctx.resume_inputs:
        exp = review.expense
        risk = review.risk_assessment
        factors = "\n  - " + "\n  - ".join(risk.risk_factors) if risk.risk_factors else " None identified"
        redacted_info = f"\n• Redacted PII: {', '.join(exp.redacted_categories)}" if exp.redacted_categories else ""

        if risk.is_security_event:
            header = "🛡️ [SECURITY EVENT - PROMPT INJECTION DETECTED]"
            action_prompt = "⚠️ High-risk security event. Review the sanitized payload and confirm rejection or investigation."
        else:
            header = "🚨 [RISK ALERT] Expense Review Required"
            action_prompt = "Please respond to approve or reject this expense."

        alert_message = (
            f"{header}\n"
            f"• Submitter: {exp.submitter}\n"
            f"• Amount: ${exp.amount:.2f} (Threshold: ${config.AUTO_APPROVE_THRESHOLD:.2f})\n"
            f"• Category: {exp.category}\n"
            f"• Date: {exp.date}\n"
            f"• Description (Sanitized): {exp.description}{redacted_info}\n"
            f"• Risk Level: {risk.risk_level}\n"
            f"• Risk Factors:{factors}\n"
            f"• Assessment: {risk.alert_summary}\n"
            f"• Recommendation: {risk.recommended_action}\n\n"
            f"{action_prompt}"
        )

        yield RequestInput(
            interrupt_id=interrupt_id,
            message=alert_message,
        )
        return

    # Resumed run: human has provided input
    human_response = ctx.resume_inputs[interrupt_id]
    status, notes = _parse_human_decision(human_response)

    outcome = ExpenseOutcome(
        expense=review.expense,
        status=status,
        reason=(
            f"Security review decision: {status}."
            if review.risk_assessment.is_security_event
            else f"Human review completed with decision: {status}."
        ),
        reviewed_by="human_approver",
        risk_alert=review.risk_assessment,
        approver_notes=notes,
        is_security_event=review.risk_assessment.is_security_event,
    )
    yield Event(output=outcome, state={"outcome": outcome.model_dump()})


def record_outcome(node_input: Any, ctx: Context | None = None) -> Event:
    """Final terminal node: logs outcome and emits user-facing content event."""
    outcome = _resolve_input(node_input, ctx, ExpenseOutcome, "outcome")

    logger.info(
        "Recorded final expense outcome: Submitter=%s, Amount=$%.2f, Status=%s, ReviewedBy=%s, SecurityEvent=%s",
        outcome.expense.submitter,
        outcome.expense.amount,
        outcome.status,
        outcome.reviewed_by,
        outcome.is_security_event,
    )

    security_badge = " [SECURITY FLAGGED]" if outcome.is_security_event else ""
    pii_badge = f"\n- **Redacted PII**: {', '.join(outcome.expense.redacted_categories)}" if outcome.expense.redacted_categories else ""

    summary_text = (
        f"📋 **Expense Report Outcome**{security_badge}\n\n"
        f"- **Submitter**: {outcome.expense.submitter}\n"
        f"- **Amount**: ${outcome.expense.amount:.2f}\n"
        f"- **Category**: {outcome.expense.category}\n"
        f"- **Status**: {'✅ ' if outcome.status == 'APPROVED' else '❌ '}{outcome.status}\n"
        f"- **Reviewed By**: {outcome.reviewed_by}\n"
        f"- **Details**: {outcome.reason}{pii_badge}\n"
    )
    if outcome.approver_notes:
        summary_text += f"- **Approver Notes**: {outcome.approver_notes}\n"

    return Event(
        output=outcome,
        content=types.Content(
            role="model",
            parts=[types.Part.from_text(text=summary_text)],
        ),
    )


# Explicitly wrap human_approval with rerun_on_resume=True
human_approval_node = node(human_approval, name="human_approval", rerun_on_resume=True)


# ------------------------------------------------------------------------------
# Workflow Graph Assembly
# ------------------------------------------------------------------------------

root_agent = Workflow(
    name="ambient_expense_agent",
    description=(
        "Ambient expense-approval workflow using ADK 2.0 graph API with "
        "deterministic routing, security checkpoint (PII scrubbing & prompt injection defense), "
        "LLM risk review, and human-in-the-loop sign-off."
    ),
    edges=[
        ("START", parse_expense),
        (parse_expense, route_expense),
        (
            route_expense,
            {
                "auto_approve": auto_approve,
                "requires_review": security_checkpoint,
            },
        ),
        (
            security_checkpoint,
            {
                "clean": review_risk,
                "security_alert": human_approval_node,
            },
        ),
        (review_risk, human_approval_node),
        (human_approval_node, record_outcome),
        (auto_approve, record_outcome),
    ],
)

app = App(
    root_agent=root_agent,
    name="app",
    resumability_config=ResumabilityConfig(is_resumable=True),
)
