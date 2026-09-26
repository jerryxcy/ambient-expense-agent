# ==============================================================================
# Ambient Expense Agent - Makefile
# ==============================================================================

PORT ?= 8080
SUBSCRIPTION ?= projects/local-project/subscriptions/expense-sub
AMOUNT ?= 250.0

.PHONY: help install playground run trigger test clean

help:
	@echo "Available commands:"
	@echo "  make install     - Install project dependencies using uv"
	@echo "  make run         - Run the ambient web service (Pub/Sub trigger) on http://localhost:$(PORT)"
	@echo "  make trigger     - Send a sample Pub/Sub push message to the running service (AMOUNT=$(AMOUNT))"
	@echo "  make playground  - Dev server with hot reload on http://localhost:$(PORT) (dev UI at /dev-ui, Pub/Sub trigger enabled)"
	@echo "  make test        - Run unit tests with pytest"
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

clean:
	find . -type d \( -name __pycache__ -o -name .pytest_cache -o -name .ruff_cache \) -not -path "./.venv/*" -prune -exec rm -rf {} +
