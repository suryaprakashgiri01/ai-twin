# Tasks: api-contract-v2

Each task is one commit (a large task may carry several). Commit subjects reference the task id.

| ID | Task | Verifies AC |
|---|---|---|
| T1 | Error catalog and envelope: renamed codes, `CATALOG`, `METHOD_NOT_ALLOWED`, `{status, message, result, error}` | 2, 3 |
| T2 | Success envelope and camelCase: `ApiModel`, `Envelope`, `ok()`, every route enveloped, deletes `200`, SSE keys camelCase, prompts keep snake_case schemas | 1, 5 |
| T3 | Paging, sorting, search and filter on every list route (`PageQuery`, `paginate`, per-route allowlists) | 4 |
| T4 | UUID v4 public ids: `uid` fields and indexes, migration 0007, uid lookups, `parse_id`, export/import | 6 |
| T5 | Clients: `scripts/chat.py`, `scripts/memory.py` and their tests; Postman generator (git-ignored) | 7 |
| T6 | Gate: `make check`; dev-notes `testing/api-contract-v2.md` (git-ignored) | 7, 8 |
