# Tasks: answer-studio

Each task is one commit (a large task may carry several). Commit subjects reference the task id.

| ID | Task | Verifies AC |
|---|---|---|
| T1 | `ollama-qwen` entry + chains, per-entry Ollama override, `prefer: local-quality`, ollama script + doctor for every Ollama entry | 1 |
| T2 | `OWNER_NAMES` allowlist in redaction | 2 |
| T3 | Owner chat: `owner-v3` prompt, document cards (+ backfill), broad mode, follow-up reuse, tier-aware history | 3 |
| T4 | `local_only` release (API, intake `release`), publish refusal | 4 |
| T5 | Prep agent: graph, route, CLI `make prep`, outward-safety filter | 5 |
| T6 | `kb_sync` loader + `refresh=true` + GitHub tier by visibility | 6 |
| T8 | Per-agent models (`config/agents.yaml`), `ollama-qwen-14b`, paid providers and the cloud tier cap | 8, 9 |
| T9 | Super agent: planner, sub-agent steps, composer, route, `make super` | 7 |
| T11 | New ingestion categories, auto-tags, Laya classifier | 10 |
| T10 | `docs/super-agent.md` (HLD), docs index | 7–10 |
| T7 | Tests, live run, dev-notes, Postman (last) | 1–11 |
