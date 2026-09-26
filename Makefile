# ==============================================================================
# Ambient Expense Agent - Makefile
# ==============================================================================

PORT ?= 8080

.PHONY: help install playground run test clean

help:
	@echo "Available commands:"
	@echo "  make install     - Install project dependencies using uv"
	@echo "  make playground  - Launch the ADK web playground on http://localhost:$(PORT)"
	@echo "  make run         - Run the standalone FastAPI server on http://localhost:8000"
	@echo "  make test        - Run unit tests with pytest"
	@echo "  make clean       - Remove cache and build artifacts"

install:
	uv sync

playground:
	uv run adk web . --port $(PORT)

run:
	uv run python app/fast_api_app.py

test:
	uv run pytest tests/unit

clean:
	rm -rf .pytest_cache .ruff_cache __pycache__ app/__pycache__ expense_agent/__pycache__ tests/__pycache__

