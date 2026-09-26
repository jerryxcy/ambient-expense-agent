# ==============================================================================
# Ambient Expense Agent - Makefile
# ==============================================================================

PORT ?= 8080
SUBSCRIPTION ?= projects/local-project/subscriptions/expense-sub
AMOUNT ?= 250.0
TRACES ?= artifacts/traces/generated_traces.json
# Gemini free tier allows 5 judge requests/min per model; raise this on a paid key.
GRADE_QPS ?= 0.08
# Judge model for the LLM-as-judge metrics (defaults to gemini-3.8-flash in the judges).
EVAL_JUDGE_MODEL ?=

.PHONY: help install playground run trigger test generate-traces grade eval clean

help:
	@echo "Available commands:"
	@echo "  make install     - Install project dependencies using uv"
	@echo "  make run         - Run the ambient web service (Pub/Sub trigger) on http://localhost:$(PORT)"
	@echo "  make trigger     - Send a sample Pub/Sub push message to the running service (AMOUNT=$(AMOUNT))"
	@echo "  make playground  - Dev server with hot reload on http://localhost:$(PORT) (dev UI at /dev-ui, Pub/Sub trigger enabled)"
	@echo "  make test        - Run unit tests with pytest"
	@echo "  make generate-traces - Run eval scenarios through the local workflow -> $(TRACES)"
	@echo "  make grade       - Grade traces with agents-cli (LLM-as-judge metrics)"
	@echo "  make eval        - generate-traces + grade"
	@echo "  make clean       - Remove cache and build artifacts"

install:
	uv sync

# Serves app.fast_api_app (dev UI + Pub/Sub trigger + subscription middleware) with hot reload.
playground:
	uv run uvicorn app.fast_api_app:app --host 127.0.0.1 --port $(PORT) --reload --reload-dir app --reload-dir expense_agent

run:
	PORT=$(PORT) uv run python app/fast_api_app.py

trigger:
	@DATA=$$(printf '{"amount": %s, "submitter": "alice@example.com", "category": "Travel", "description": "Client visit train ticket", "date": "2026-09-26"}' "$(AMOUNT)" | base64 | tr -d '\n'); \
	curl -sS -X POST "http://localhost:$(PORT)/apps/app/trigger/pubsub" \
		-H "Content-Type: application/json" \
		-d "{\"message\": {\"data\": \"$$DATA\", \"messageId\": \"local-$$(date +%s)\"}, \"subscription\": \"$(SUBSCRIPTION)\"}"; \
	echo

test:
	uv run pytest tests/unit

generate-traces:
	uv run python tests/eval/generate_traces.py --output $(TRACES)

# agents-cli's eval SDK constructs GCS/BigQuery clients even for local-only metrics, which
# needs ADC and a project. Without ADC, point it at a token-less placeholder; it is never
# used to call GCP (local traces, local judges on GEMINI_API_KEY).
grade:
	@if [ -z "$$GOOGLE_APPLICATION_CREDENTIALS" ] && [ ! -f "$$HOME/.config/gcloud/application_default_credentials.json" ]; then \
		mkdir -p artifacts; \
		printf '{"type": "authorized_user", "client_id": "placeholder", "client_secret": "placeholder", "refresh_token": "placeholder"}' > artifacts/.placeholder_adc.json; \
		export GOOGLE_APPLICATION_CREDENTIALS=artifacts/.placeholder_adc.json; \
		export GOOGLE_CLOUD_PROJECT=$${GOOGLE_CLOUD_PROJECT:-local-eval-placeholder}; \
	fi; \
	$(if $(EVAL_JUDGE_MODEL),EVAL_JUDGE_MODEL=$(EVAL_JUDGE_MODEL)) agents-cli eval grade --traces $(TRACES) --config tests/eval/eval_config.yaml --output artifacts/grade_results --qps $(GRADE_QPS)

eval: generate-traces grade

clean:
	find . -type d \( -name __pycache__ -o -name .pytest_cache -o -name .ruff_cache \) -not -path "./.venv/*" -prune -exec rm -rf {} +
