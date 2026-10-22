# Thin wrappers: every target runs a Python script, so Windows (scripts/dev.ps1) behaves the same.
# Pass extra arguments with ARGS, e.g.  make logs ARGS="mongodb --follow"
# Keep this file free of Go-template braces and shell logic; put logic in scripts/.

PY := uv run python -m

.PHONY: help setup doctor models ollama up down status logs cleanup check run test keys export import seed kb-sync chat chat-list chat-continue twin prep super memory migrate-status upgrade downgrade graph-update graph-query graph-explain graph-path

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

ollama: ## Native Ollama: ARGS="status" | "logs" | "install" | "start" | "stop" | "pull [model]" | "smoke"
	$(PY) scripts.ollama $(ARGS)

graph-update: ## Rebuild the code graph (graphify update .)
	$(PY) scripts.graph update $(ARGS)

graph-query: ## Query the code graph: ARGS="your question"
	$(PY) scripts.graph query $(ARGS)

graph-explain: ## Explain a concept from the graph: ARGS="concept"
	$(PY) scripts.graph explain $(ARGS)

graph-path: ## Path between two symbols: ARGS="A B"
	$(PY) scripts.graph path $(ARGS)

keys: ## API keys: ARGS="create NAME --scope admin" | "list" | "revoke NAME"
	$(PY) scripts.keys $(ARGS)

seed: ## Fake-persona demo data via the API: dry run; ARGS="--apply" | "--remove --apply"
	$(PY) scripts.seed $(ARGS)

kb-sync: ## Build/refresh the KB from vault/kb/manifest.yaml: dry run; ARGS="--apply" | "cards --apply" | "--reset --apply"
	$(PY) scripts.kb_sync $(ARGS)

export: ## Encrypted vault archive under vault/exports/ (prompts for a passphrase)
	$(PY) scripts.export $(ARGS)

import: ## Dry run of ARGS="vault/exports/X.atv"; add --apply to restore
	$(PY) scripts.import_ $(ARGS)

check: ## Quality gate: ruff, mypy strict, pytest + coverage
	$(PY) scripts.check

chat: ## Chat with the vault in the terminal (new conversation; ARGS="rename X new-name")
	$(PY) scripts.chat $(ARGS)

twin: ## Add knowledge in plain language (intake assistant; confirms before every write)
	$(PY) scripts.chat twin $(ARGS)

super: ## The super agent: one conversation that saves, answers and prepares (local only)
	$(PY) scripts.chat super $(ARGS)

prep: ## Interview and application answers in your voice (first person, from your vault)
	$(PY) scripts.chat prep $(ARGS)

chat-list: ## List conversations: name, mode, id, updated, turns, description
	$(PY) scripts.chat list $(ARGS)

chat-continue: ## Continue a conversation (ask or intake, at a pending draft): ARGS="<name|id>"
	$(PY) scripts.chat continue $(ARGS)

memory: ## What the agents remember: ARGS="list [--pending]" | "add TEXT" | "accept ID" | "edit ID TEXT" | "forget ID"
	$(PY) scripts.memory $(ARGS)

migrate-status: ## MongoDB data migrations: applied / pending, reversible or not
	$(PY) scripts.migrate status $(ARGS)

upgrade: ## Apply pending MongoDB migrations (the API also does this at every start)
	$(PY) scripts.migrate upgrade $(ARGS)

downgrade: ## Roll back migrations newer than a version (dry run): ARGS="0003 --apply"
	$(PY) scripts.migrate downgrade $(ARGS)

# On Windows, Ctrl+C ends `make` before the server has finished stopping, so PowerShell prints
# its prompt above the last shutdown lines and the cursor looks stuck: press Enter. Running the
# server through scripts/dev.ps1 (no make in between) avoids it.
ifeq ($(OS),Windows_NT)
RUN_HINT = @echo "Tip (Windows): ./scripts/dev.ps1 run stops cleanly on Ctrl+C; with make, press Enter after it stops."
else
RUN_HINT = @true
endif

run: ## Run the API locally (reload)
	$(RUN_HINT)
	$(PY) app.main --reload

test: ## Tests only (no coverage gate)
	uv run pytest $(ARGS)
