# Tasks: ingestion

Each task is one commit (a large task may carry several). Commit subjects reference the task id.

| ID | Task | Verifies AC |
|---|---|---|
| T1 | `MongoOverrideStore` wired into the orchestrator | 1 |
| T2 | Data model additions + migration (job states, quarantine, redaction report, canaries, profile resume) | — |
| T3 | Parsers: dispatch by content type, PDF, office/markitdown, text, image OCR; `vault` dependency group | 2 |
| T4 | Fetchers: SSRF-safe URL fetch + Docs rewrite, GitHub allowlist sync, LinkedIn export ZIP | 10, 11 |
| T5 | Redaction (Presidio + Indian-ID recognisers + allowlist) and employer guard | 3, 4 |
| T6 | Chunking + labelling (rules, confidential-tier classifier, max-merge) | 5, 6 |
| T7 | Embedding (fastembed dense + sparse), canaries, pipeline stages, worker pool, restart recovery | 7, 8, 12 |
| T8 | Admin ingest routes + source status | 2, 8 |
| T9 | Review/publish/unpublish/relabel/delete routes with audit | 9 |
| T10 | Profile and records routes | — |
| T11 | Tests (unit + integration), `doctor`/`setup` spaCy model check, dev-notes E2E for macOS and Windows | 1–13 |
