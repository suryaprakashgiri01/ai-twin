# Thin wrappers: every target runs a Python script, so Windows (scripts/dev.ps1) behaves the same.
# Pass extra arguments with ARGS, e.g.  make logs ARGS="mongodb --follow"
# Keep this file free of Go-template braces and shell logic; put logic in scripts/.

PY := uv run python -m

.PHONY: help setup doctor models up down status logs cleanup check run test keys export import

help: ## List targets
	@grep -E '^[a-z-]+:.*## ' Makefile | sed 's/:.*## /\t/'

setup: ## First-time setup: .env, deps, git hooks, stack
	$(PY) scripts.setup $(ARGS)

doctor: ## Check tools, compose runtime, .env and ports
	$(PY) scripts.doctor $(ARGS)

models: ## Download local models (spaCy; ARGS="--embeddings" adds fastembed)
	$(PY) scripts.models $(ARGS)

up: ## Start the local stack and wait for health
	$(PY) scripts.up $(ARGS)

down: ## Stop the local stack (data kept)
	$(PY) scripts.down $(ARGS)

status: ## Show container state and health
	$(PY) scripts.status $(ARGS)

logs: ## Container logs (ARGS="service --follow")
	$(PY) scripts.logs $(ARGS)

cleanup: ## Dry run; ARGS="--apply" deletes containers, volumes, caches
	$(PY) scripts.cleanup $(ARGS)

keys: ## API keys: ARGS="create NAME --scope admin" | "list" | "revoke NAME"
	$(PY) scripts.keys $(ARGS)

export: ## Encrypted vault archive under vault/exports/ (prompts for a passphrase)
	$(PY) scripts.export $(ARGS)

import: ## Dry run of ARGS="vault/exports/X.atv"; add --apply to restore
	$(PY) scripts.import_ $(ARGS)

check: ## Quality gate: ruff, mypy strict, pytest + coverage
	$(PY) scripts.check

run: ## Run the API locally (reload)
	$(PY) app.main --reload

test: ## Tests only (no coverage gate)
	uv run pytest $(ARGS)
