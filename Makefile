CONDA_ENV := codingtest
PYTHON := conda run -n $(CONDA_ENV) python

.DEFAULT_GOAL := help

.PHONY: help install install-browser demo-app schema replay test test-browser lint verify

ARTIFACT ?= artifacts/open-sub-account-review-v1.yaml
MEMBER ?= 58431
TYPE ?= savings

help: ## Show available commands
	@awk 'BEGIN {FS = ":.*## "; printf "Usage: make <target>\n\n"} /^[a-zA-Z_-]+:.*## / {printf "  %-14s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

install: ## Install the project and test dependencies into the codingtest Conda environment
	conda run -n $(CONDA_ENV) python -m pip install -e '.[dev]'

install-browser: ## Install the pinned Playwright Chromium build
	$(PYTHON) -m playwright install chromium

demo-app: ## Run Meridian CU at http://127.0.0.1:8000
	$(PYTHON) -m uvicorn demo_app.app:app --host 127.0.0.1 --port 8000

schema: ## Emit the capability JSON Schema to artifacts/schema.json
	$(PYTHON) -m interface_cua.schema.artifact artifacts/schema.json

replay: ## Replay a capability against the running demo app (ARTIFACT/MEMBER/TYPE, needs `make demo-app`)
	env -u ANTHROPIC_API_KEY $(PYTHON) -m interface_cua.cli replay $(ARTIFACT) \
		--input member_id=$(MEMBER) --input account_type=$(TYPE) --allow-draft

test: ## Run automated tests without an Anthropic API key
	env -u ANTHROPIC_API_KEY conda run -n $(CONDA_ENV) pytest

test-browser: ## Run real Chromium replay tests without an Anthropic API key
	env -u ANTHROPIC_API_KEY conda run -n $(CONDA_ENV) pytest tests/test_browser_replay.py

lint: ## Run Ruff checks
	conda run -n $(CONDA_ENV) ruff check .

verify: lint test schema ## Run all offline verification
