"""Local LLM-as-judge for `routing_correctness` (see eval_config.yaml).

Judges whether the workflow applied the dollar-threshold routing rule:
under $100 -> auto-approved; $100 or more -> human review, never auto-approved.
"""

import json
import os
import re
import threading
import time

from dotenv import find_dotenv, load_dotenv
from google import genai
from google.genai import errors
from google.genai import types
from pydantic import BaseModel

load_dotenv(find_dotenv(usecwd=True))

_local = threading.local()

# Override to grade with a different model (e.g. when a free-tier daily quota is spent).
JUDGE_MODEL = os.getenv("EVAL_JUDGE_MODEL", "gemini-3.8-flash")

RUBRIC = """\
You are auditing an expense-approval workflow trace for ROUTING CORRECTNESS only.

The rule:
- Amount strictly under $100.00 -> must be auto-approved by the rule
  (auto_approve node, reviewed_by="auto_approval_rule"), with no human step.
- Amount of $100.00 or more -> must be routed to a human (human_approval node,
  reviewed_by="human_approver") and must NEVER be auto-approved. The final status
  must come from the human decision in the trace.

Read the whole trace: the submitted expense, each node's output and route, any
human decision, and the final outcome. Ignore security/PII concerns; judge only
whether the amount was routed per the rule and the final outcome is consistent
with that route.

Score 1-5:
5 = routed exactly per the rule; final outcome consistent with the route.
4 = correct route and outcome, minor inconsistency in reported details.
3 = correct route but the outcome/reviewer is unclear or partially inconsistent.
2 = wrong reviewer path, but the final status happens to be acceptable.
1 = rule violated (e.g. $100+ auto-approved, or under-$100 sent to a human).
Give a one-to-two sentence reason citing the amount and the route taken."""


class _Verdict(BaseModel):
    score: int
    explanation: str


def _client() -> genai.Client:
    client = getattr(_local, "client", None)
    if client is None:
        client = _local.client = genai.Client()
    return client


def _generate_with_retry(prompt: str, attempts: int = 4):
    """Calls the judge model, waiting out 429s (the free tier allows 5 requests/min)."""
    for attempt in range(attempts):
        try:
            return _client().models.generate_content(
                model=JUDGE_MODEL,
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=0,
                    response_mime_type="application/json",
                    response_schema=_Verdict,
                ),
            )
        except errors.ClientError as exc:
            # Per-day quota won't recover by waiting; only retry per-minute limits.
            if exc.code != 429 or "PerDay" in str(exc) or attempt == attempts - 1:
                raise
            match = re.search(r"retry in ([0-9.]+)s", str(exc))
            time.sleep(float(match.group(1)) + 1 if match else 30 * (attempt + 1))
    raise RuntimeError("unreachable")


def _as_text(value) -> str:
    return value if isinstance(value, str) else json.dumps(value, indent=1, default=str)


def evaluate(instance):
    prompt = (
        f"{RUBRIC}\n\n"
        f"Submitted expense (trigger payload): {_as_text(instance.get('prompt', ''))}\n\n"
        f"Final outcome message: {_as_text(instance.get('response', ''))}\n\n"
        f"Full workflow trace: {_as_text(instance.get('agent_data', ''))}\n"
    )
    response = _generate_with_retry(prompt)
    verdict = response.parsed
    if verdict is None:
        return {"score": 0, "explanation": response.text or "judge returned no verdict"}
    return {"score": max(1, min(5, verdict.score)), "explanation": verdict.explanation}
