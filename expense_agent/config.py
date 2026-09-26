"""Configuration for the ambient expense-approval agent."""

import os

# Auto-approval dollar threshold: expenses strictly below this amount are auto-approved.
# Expenses at or above this threshold require LLM risk analysis and human approval.
AUTO_APPROVE_THRESHOLD: float = float(os.getenv("AUTO_APPROVE_THRESHOLD", "100.0"))

# Gemini model for risk factor analysis (default: gemini-3.1-flash-lite)
MODEL: str = os.getenv("EXPENSE_AGENT_MODEL", "gemini-3.1-flash-lite")

# Interrupt identifier used by RequestInput for the human review step
APPROVAL_INTERRUPT_ID: str = "expense_approval_decision"
