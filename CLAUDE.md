# CLAUDE.md

Ambient expense-approval agent built on the ADK 2.0 graph `Workflow` API, scaffolded with `agents-cli` (ADK template, Agent Runtime target, A2A enabled). Follows the Google codelab "Vibecode an ambient expense agent".

`GEMINI.md` is the agents-cli guide for Gemini-based coding agents; this file mirrors it and adds project-specific rules. Keep the two in sync when changing workflow or commands.

## Prerequisites

Install the CLI (one-time):
```bash
uv tool install google-agents-cli
```

The `google-agents-cli-*` skills (workflow, adk-code, eval, deploy, observability, publish, scaffold) cover agents-cli usage in depth. Load the relevant one before scaffolding, evaluating, or deploying.

## Architecture

- `app/agent.py` is a thin entrypoint (`agent_directory` in `agents-cli-manifest.yaml`) that re-exports `app` and `root_agent` from `expense_agent`. **Agent logic lives in `expense_agent/`, not `app/`.**
- `expense_agent/agent.py`: graph nodes and `Workflow` wiring:
  `START → parse_expense → route_expense → {auto_approve | security_checkpoint}`,
  `security_checkpoint → {clean: review_risk | security_alert: human_approval}`,
  `review_risk → human_approval → record_outcome`, `auto_approve → record_outcome`.
- `expense_agent/security.py`: PII scrubbing (SSN, credit card) and regex-based prompt-injection detection.
- `expense_agent/models.py`: Pydantic models (`Expense`, `RiskAssessment`, `ExpenseReview`, `ExpenseOutcome`).
- `expense_agent/config.py`: `AUTO_APPROVE_THRESHOLD` (default 100.0) and `MODEL`, both overridable by env var.
- `app/fast_api_app.py` enables ADK's built-in Pub/Sub trigger (`trigger_sources=["pubsub"]`); `app/app_utils/pubsub.py` middleware shortens `projects/<p>/subscriptions/<s>` to `<s>` so session `user_id`s stay readable. Telemetry is local only (`otel_to_cloud=False`); logs go to the console via standard `logging`.
- `human_approval` pauses with `RequestInput` and is wrapped with `rerun_on_resume=True`; the `App` uses `ResumabilityConfig(is_resumable=True)`.

## Security Invariants

Preserve these when changing nodes:

- **Scrub PII before persisting**: `parse_expense` scrubs PII before writing `ctx.state` or logging. Never store or log a raw description.
- **Fail closed**: nodes resolve input via `_resolve_input`, which raises when data is missing. Never fabricate a default `Expense` or an `APPROVED` outcome.
- **Ambiguous human replies reject**: `_parse_human_decision` returns `REJECTED` unless the reply clearly approves.
- **Screen every prompt field**: prompt-injection checks cover every field interpolated into the LLM prompt (`submitter`, `category`, `date`, `description`). If you add a field to the prompt, add it to the check.
- **Injection skips the LLM**: detected injection routes straight to `human_approval`; the LLM never sees it.

## Development Phases

1. **Understand requirements**: understand requirements, constraints, and success criteria before writing code.
2. **Build and implement**: implement logic in `expense_agent/`. Use `agents-cli playground` (or `make playground`) for interactive testing. Iterate on user feedback.
3. **Evaluation loop (main iteration phase)**: start with 1-2 eval cases, run `agents-cli eval run`, and iterate until satisfied (expect 5-10+ iterations). With a baseline, use `agents-cli eval compare` (regressions), `agents-cli eval analyze` (failure clusters), and `agents-cli eval optimize` (prompt tuning). Datasets live in `tests/eval/datasets/`.
4. **Pre-deployment tests**: run `uv run pytest tests/unit tests/integration` until everything passes.
5. **Deploy to dev**: **requires explicit human approval.** Run `agents-cli deploy` only after the user confirms.
6. **Production deployment**: ask the user whether they want Option A (simple single-project) or Option B (full CI/CD with `agents-cli infra cicd`).

## Commands

| Command | Purpose |
|---------|---------|
| `make install` / `uv sync` | Install dependencies |
| `make test` | Unit tests only (`uv run pytest tests/unit`) |
| `uv run pytest tests/unit tests/integration` | Unit + integration tests |
| `agents-cli playground` | Interactive local testing |
| `make playground` | Hot-reload dev server on :8080 (dev UI at `/dev-ui`, Pub/Sub trigger enabled) |
| `make run` | Ambient web service on :8080; Pub/Sub push endpoint `POST /apps/app/trigger/pubsub` |
| `make trigger AMOUNT=250` | Send a sample Pub/Sub push message to the running service |
| `make eval` | `make generate-traces` (local Runner, auto-answers human approval) + `make grade` (routing_correctness, security_containment judges) |
| `make grade EVAL_JUDGE_MODEL=gemini-3.1-flash-lite` | Re-grade with another judge model (free tier: 20 req/day/model) |
| `agents-cli eval dataset synthesize` | Synthesize multi-turn eval scenarios |
| `agents-cli eval run` | Run the agent over the eval dataset and grade traces |
| `agents-cli eval generate` / `agents-cli eval grade` | Decoupled form: produce traces, then grade |
| `agents-cli eval compare` | Compare two grade-results files |
| `agents-cli eval analyze` | Cluster failure modes |
| `agents-cli eval metric list` | List built-in metrics |
| `agents-cli eval optimize` | Auto-tune prompts using eval data |
| `agents-cli lint` | Check code quality |
| `agents-cli infra single-project` | Set up infrastructure (Terraform) |
| `agents-cli deploy` | Deploy to dev (needs approval) |
| `agents-cli scaffold enhance` | Add deployment target or CI/CD |
| `agents-cli scaffold upgrade` | Upgrade to the latest template |

## Operational Guidelines

- **Code preservation**: only modify code the request targets. Preserve surrounding code, config values (e.g. `model`), comments, and formatting.
- **Never change the model** (`EXPENSE_AGENT_MODEL` / `config.MODEL`) unless explicitly asked.
- **Model 404 errors**: fix `GOOGLE_CLOUD_LOCATION` (e.g. `global` instead of `us-east1`), not the model name.
- **ADK tool imports**: import the tool instance, not the module: `from google.adk.tools.load_web_page import load_web_page`.
- **Run Python with `uv`**: `uv run python script.py`. Run `agents-cli install` first.
- **Stop on repeated errors**: if the same error appears 3+ times, fix the root cause instead of retrying.
- **Terraform conflicts** (Error 409): use `terraform import` instead of retrying creation.
- **Secrets**: `.env` holds the real `GEMINI_API_KEY` and is gitignored. Never commit it. Put new variables in `.env.example` with placeholder values.
