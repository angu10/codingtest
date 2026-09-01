# Uses whatever `python` is on PATH, so activate your virtualenv first.
# For a specific interpreter:  make test PYTHON=/path/to/python
PYTHON ?= python

.DEFAULT_GOAL := help

.PHONY: help install install-browser demo-app schema replay replay-create evidence test test-browser lint verify

ARTIFACT ?= artifacts/open-sub-account-review-v1.yaml
MEMBER ?= 58431
TYPE ?= savings
NICKNAME ?= Rainy Day
DEPOSIT ?= 250.00
#: Extra CLI flags, e.g. FLAGS="--headed --handoff" or FLAGS="--evidence evidence"
FLAGS ?=

help: ## Show available commands
	@awk 'BEGIN {FS = ":.*## "; printf "Usage: make <target>\n\n"} /^[a-zA-Z_-]+:.*## / {printf "  %-14s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

install: ## Install the project and its test dependencies into the active environment
	$(PYTHON) -m pip install -e '.[dev]'

install-browser: ## Install the pinned Playwright Chromium build
	$(PYTHON) -m playwright install chromium

demo-app: ## Run Meridian CU at http://127.0.0.1:8000
	$(PYTHON) -m uvicorn demo_app.app:app --host 127.0.0.1 --port 8000

schema: ## Emit the capability JSON Schema to artifacts/schema.json
	$(PYTHON) -m interface_cua.schema.artifact artifacts/schema.json

replay: ## Replay a capability against the running demo app (ARTIFACT/MEMBER/TYPE/FLAGS, needs `make demo-app`)
	env -u ANTHROPIC_API_KEY $(PYTHON) -m interface_cua.cli replay $(ARTIFACT) \
		--input member_id=$(MEMBER) --input account_type=$(TYPE) --allow-draft $(FLAGS)

replay-create: ## Replay the consequential write (MEMBER=55505 is the ambiguous-response case)
	env -u ANTHROPIC_API_KEY $(PYTHON) -m interface_cua.cli replay artifacts/create-sub-account-v1.yaml \
		--input member_id=$(MEMBER) --input account_type=$(TYPE) --input nickname='$(NICKNAME)' \
		--input opening_deposit=$(DEPOSIT) --allow-draft --confirm submit-create $(FLAGS)

evidence: ## Write the replay evidence the submission ships (needs `make demo-app`)
	env -u ANTHROPIC_API_KEY $(PYTHON) -m interface_cua.cli replay $(ARTIFACT) \
		--input member_id=58431 --input account_type=savings --allow-draft \
		--evidence evidence --run-id replay-success
	-env -u ANTHROPIC_API_KEY $(PYTHON) -m interface_cua.cli replay $(ARTIFACT) \
		--input member_id=55506 --input account_type=savings --allow-draft \
		--evidence evidence --run-id replay-undeclared-refusal

test: ## Run the whole suite with ANTHROPIC_API_KEY removed from the environment
	env -u ANTHROPIC_API_KEY $(PYTHON) -m pytest

test-browser: ## Run only the real-Chromium replay tests, still with no API key
	env -u ANTHROPIC_API_KEY $(PYTHON) -m pytest tests/test_browser_replay.py

lint: ## Run Ruff checks
	$(PYTHON) -m ruff check .

verify: lint test schema ## Run all offline verification
