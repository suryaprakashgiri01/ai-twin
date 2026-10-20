# Plan: api-contract-v2

## Shape
```
route handler ─▶ service (unchanged lists / objects, snake_case, internal ids)
      │
      ├─ single object ─▶ ok(obj, "message") ─▶ Envelope[T]          (FastAPI dumps by alias → camelCase)
      └─ list ──────────▶ paginate(items, PageQuery) ─▶ Envelope[Page[T]]
AppError / validation / HTTP / unhandled ─▶ handlers._envelope ─▶ {"status":"error", …, "error":{code,…}}
SSE: streaming.sse(event, data) ─▶ camelCase keys
ids: path uid ─▶ parse_id (UUID v4) ─▶ store lookup by `uid` ─▶ internal `_id` stays internal
```

## Module layout

| Module | Responsibility |
|---|---|
| `app/exceptions/codes.py` | `ErrorCode`, `CATALOG` (status + default message), `STATUS_BY_CODE`, `CODE_BY_HTTP_STATUS` |
| `app/exceptions/handlers.py` | the error envelope; validation details without echoed input |
| `app/api/envelope.py` | `ApiModel` (camelCase base), `Envelope[T]`, `Page[T]`, `Deleted`, `ok()`, `camel_keys()` |
| `app/api/paging.py` | `PageQuery`, `page_query(...)` dependency factory (allowlists), `paginate()` |
| `app/api/routes/*` | return envelopes; list routes take `PageQuery` |
| `app/services/*`, `app/leads/*`, `app/security/*` | API models extend `ApiModel`; stores look up by `uid` |
| `app/db/documents.py` | `uid` fields + partial unique indexes; `source_uid`, `resume_document_uid` |
| `app/migrations/m0007_public_ids.py` | backfill `uid`s and references; `down` unsets them |
| `scripts/_vault.py`, `scripts/export.py`, `scripts/import_.py` | keep `uid`; add one when an old export lacks it |
| `scripts/chat.py`, `scripts/memory.py` | unwrap `result`, read pages and camelCase keys |
| `dev-notes/postman/build_postman.py` (git-ignored) | envelope-aware examples and paging params |

## Decisions
- **Explicit envelopes over middleware.** Handlers return `Envelope[T]`, so OpenAPI shows the real shape and
  mypy checks it. A body-rewriting middleware would hide the contract and break streaming.
- **Paging in the API layer.** Services keep returning lists (the intake agent uses them too). Filters with
  an index (`status`, `kind`, `triage`, `severity`) are pushed to the store; the rest is in-memory over at
  most `LIST_SCAN_MAX` rows.
- **`uid` beside `_id`.** Internal references (chunks, vectors, canaries, the outbox, alert marks) keep
  ObjectIds, so nothing is re-indexed. Only the API boundary translates. Partial unique indexes
  (`uid` is a string) let startup create them before migration 0007 has run.
- **Prompts stay snake_case.** Models whose JSON Schema goes into an intake prompt are rendered with
  `by_alias=False`, so the prompt text (and `PROMPT_VERSION`) do not change.
- **Malformed ids are 404**, as before: an id that is not a UUID cannot name anything.

## Risks
- Every client breaks at once. Mitigation: one PR updates the CLI, Postman and tests together; the front end
  is not live yet.
- A missed model keeps snake_case. Mitigation: a test walks the OpenAPI schema of `/admin/v1` and `/v1`
  responses and fails on any property containing `_`.
- Migration 0007 on a large `security_events` collection. Mitigation: batched `bulk_write`, idempotent
  (only documents without `uid`).
