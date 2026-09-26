"""Generates eval traces by running each dataset case through the local ADK workflow.

Unlike `agents-cli eval generate`, this drives the Runner in-process so it can:
  * answer the human_approval interrupt automatically (approve clean expenses,
    reject security events), resuming the paused workflow;
  * record the exact prompt sent to Gemini in review_risk, so judges can verify
    PII never reached the model.

Usage:
    uv run python tests/eval/generate_traces.py [--dataset PATH] [--output PATH]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from google.adk.events.event import Event
from google.adk.runners import Runner
from google.adk.sessions.in_memory_session_service import InMemorySessionService
from google.genai import types

load_dotenv()

from expense_agent import agent as expense_agent_module  # noqa: E402
from expense_agent import app  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATASET = REPO_ROOT / "tests/eval/datasets/basic-dataset.json"
DEFAULT_OUTPUT = REPO_ROOT / "artifacts/traces/generated_traces.json"
REQUEST_INPUT_NAME = "adk_request_input"
LLM_EVENT_AUTHOR = "review_risk"

logger = logging.getLogger("generate_traces")


# ------------------------------------------------------------------------------
# LLM call recording
# ------------------------------------------------------------------------------

class _RecordingModels:
    """Wraps client.aio.models to log each generate_content call into the trace."""

    def __init__(self, models: Any, sink: list[dict[str, Any]]) -> None:
        self._models = models
        self._sink = sink

    async def generate_content(self, *, model: str, contents: Any, **kwargs: Any) -> Any:
        self._sink.append(
            _text_event(
                LLM_EVENT_AUTHOR,
                f"[LLM request] model={model}\nprompt sent to model:\n{contents}",
            )
        )
        response = await self._models.generate_content(model=model, contents=contents, **kwargs)
        self._sink.append(_text_event(LLM_EVENT_AUTHOR, f"[LLM response]\n{response.text}"))
        return response

    def __getattr__(self, name: str) -> Any:
        return getattr(self._models, name)


class _RecordingClient:
    def __init__(self, client: Any, sink: list[dict[str, Any]]) -> None:
        self._client = client
        self.aio = type("Aio", (), {"models": _RecordingModels(client.aio.models, sink)})()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)


# ------------------------------------------------------------------------------
# Event serialization
# ------------------------------------------------------------------------------

def _text_event(author: str, text: str) -> dict[str, Any]:
    return {"author": author, "content": {"role": "model", "parts": [{"text": text}]}}


def _node_name(event: Event) -> str:
    path = event.node_info.path if event.node_info else ""
    return path.rsplit("/", 1)[-1].split("@", 1)[0] if path else app.root_agent.name


def _serialize(event: Event) -> dict[str, Any] | None:
    """Converts an ADK event into an eval AgentEvent dict; drops bookkeeping events."""
    author = _node_name(event)
    content = (
        event.content.model_dump(mode="json", exclude_none=True) if event.content else None
    )
    output = getattr(event, "output", None)
    route = event.actions.route if event.actions else None

    if output is not None and content is None:
        payload = output.model_dump(mode="json") if hasattr(output, "model_dump") else output
        header = f"[{author} output" + (f", route={route}" if route else "") + "]"
        content = {
            "role": "model",
            "parts": [{"text": f"{header}\n{json.dumps(payload, indent=2)}"}],
        }

    state_delta = dict(event.actions.state_delta) if event.actions and event.actions.state_delta else None
    if content is None and not state_delta:
        return None

    serialized: dict[str, Any] = {"author": author}
    if content:
        serialized["content"] = content
    if state_delta:
        serialized["state_delta"] = state_delta
    return serialized


def _find_interrupt(event: Event) -> str | None:
    for part in (event.content.parts if event.content and event.content.parts else []):
        if part.function_call and part.function_call.name == REQUEST_INPUT_NAME:
            return part.function_call.id
    return None


# ------------------------------------------------------------------------------
# Case execution
# ------------------------------------------------------------------------------

async def _run_turn(
    runner: Runner,
    session_id: str,
    message: types.Content,
    sink: list[dict[str, Any]],
    invocation_id: str | None = None,
    skip_authors: frozenset[str] = frozenset(),
) -> tuple[str | None, str | None]:
    """Runs one turn, appending serialized events to sink. Returns (interrupt_id, invocation_id).

    Events from nodes in skip_authors are dropped: on resume ADK replays the outputs of
    nodes that already completed, which would otherwise look like a second execution.
    """
    interrupt_id = None
    last_invocation = invocation_id
    async for event in runner.run_async(
        user_id="eval",
        session_id=session_id,
        new_message=message,
        invocation_id=invocation_id,
    ):
        last_invocation = event.invocation_id
        interrupt_id = _find_interrupt(event) or interrupt_id
        if (serialized := _serialize(event)) is not None and serialized["author"] not in skip_authors:
            sink.append(serialized)
    return interrupt_id, last_invocation


def _automated_decision(review: dict[str, Any]) -> dict[str, str]:
    """Approves clean reviews and rejects prompt injections (the eval's stand-in approver).

    Keys off the security checkpoint's PROMPT_INJECTION_DETECTED factor rather than
    is_security_event, which the LLM reviewer can also set on its own.
    """
    if "PROMPT_INJECTION_DETECTED" in review.get("risk_assessment", {}).get("risk_factors", []):
        return {"decision": "REJECT", "notes": "Automated eval approver: security event rejected."}
    return {"decision": "APPROVE", "notes": "Automated eval approver: clean expense approved."}


async def run_case(case: dict[str, Any]) -> dict[str, Any]:
    session_service = InMemorySessionService()
    runner = Runner(app=app, session_service=session_service)
    session = await session_service.create_session(app_name=app.name, user_id="eval")

    sink: list[dict[str, Any]] = []
    real_get_client = expense_agent_module._get_genai_client
    expense_agent_module._get_genai_client = lambda: _RecordingClient(real_get_client(), sink)
    try:
        prompt = types.Content.model_validate(case["prompt"])
        turns = [{"turn_index": 0, "turn_id": "turn_0", "events": sink}]
        sink.insert(0, {"author": "user", "content": case["prompt"]})
        interrupt_id, invocation_id = await _run_turn(runner, session.id, prompt, sink)

        if interrupt_id:
            session = await session_service.get_session(
                app_name=app.name, user_id="eval", session_id=session.id
            )
            decision = _automated_decision(session.state.get("review", {}))
            reply = types.Content(
                role="user",
                parts=[
                    types.Part(
                        function_response=types.FunctionResponse(
                            id=interrupt_id, name=REQUEST_INPUT_NAME, response=decision
                        )
                    )
                ],
            )
            sink = []
            sink.append({"author": "user", "content": reply.model_dump(mode="json", exclude_none=True)})
            expense_agent_module._get_genai_client = lambda: _RecordingClient(real_get_client(), sink)
            completed = frozenset(
                e["author"] for e in turns[0]["events"] if e["author"] not in ("user", "human_approval")
            )
            await _run_turn(
                runner, session.id, reply, sink, invocation_id=invocation_id, skip_authors=completed
            )
            turns.append({"turn_index": 1, "turn_id": "turn_1", "events": sink})
    finally:
        expense_agent_module._get_genai_client = real_get_client

    final_text = next(
        (
            "".join(p["text"] for p in e["content"]["parts"] if p.get("text"))
            for turn in reversed(turns)
            for e in reversed(turn["events"])
            if e["author"] == "record_outcome" and e.get("content")
        ),
        None,
    )

    traced = {k: v for k, v in case.items()}
    traced["agent_data"] = {
        "agents": {
            app.root_agent.name: {
                "agent_id": app.root_agent.name,
                "agent_type": "Workflow",
                "description": app.root_agent.description,
            }
        },
        "turns": turns,
    }
    if final_text:
        traced["responses"] = [{"response": {"role": "model", "parts": [{"text": final_text}]}}]
    return traced


async def main(dataset: Path, output: Path) -> None:
    cases = json.loads(dataset.read_text())["eval_cases"]
    traced_cases = []
    for case in cases:
        logger.info("Running case %s", case.get("eval_case_id"))
        traced_cases.append(await run_case(case))

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({"eval_cases": traced_cases}, indent=2, default=str))
    logger.info("Wrote %d traces to %s", len(traced_cases), output)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    asyncio.run(main(args.dataset, args.output))
