# Tasks: llm-orchestrator

Each task is one commit (a large task may carry several). Commit subjects reference the task id.

| ID | Task | Verifies AC |
|---|---|---|
| T1 | uv workspace + `packages/llm-orchestrator` skeleton; types (`Tier`, `Role`, `ModelSpec`, results, errors) | — |
| T2 | Inventory: YAML schema + validation, `config/models.yaml`, `OverrideStore` + in-memory store, merge rules | 1, 7 |
| T3 | Policy + selection: eligibility, role chains, `prefer`, privileged overrides with reasons | 2, 4 |
| T4 | Providers: LangChain builders (Groq, Cerebras, Gemini, OpenRouter, Ollama, mock), missing key = disabled | 9 |
| T5 | Executor + facade: retries, error classification, fallback, `chat`, `structured` with one repair, attempt logs | 3, 5, 8 |
| T6 | Classifiers: protocol, LLM classifier, Jev adapter, flagged classifier | 6 |
| T7 | App wiring: settings, `app/core/llm.py`, `/readyz` model counts, `.env.example` | — |
| T8 | Tests across the package and app wiring (fakes only, no network) | 1–9 |
