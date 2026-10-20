# Spec: api-contract-v2

Status: draft · Owner: Surya Prakash Giri · Ticket: none (personal repo; `tasks.md` is the task list)

## Problem
The admin (`/admin/v1`) and public (`/v1`) APIs grew route by route, and it shows:
- Success bodies have no common shape: a bare object, a bare list, `{"items": [...]}` or `204` with no body.
- Lists take an ad-hoc `limit` (sometimes `status`, `kind`, `triage`, `severity`), with no page, no total
  and no sort choice.
- JSON is snake_case, while the portfolio front end and Postman users expect camelCase.
- Error codes mix styles (`VALIDATION_ERROR`, `AUTH_REQUIRED`, `DEPENDENCY_UNAVAILABLE`) and the catalog
  of codes, statuses and meanings lives only in code.
- Public ids are MongoDB ObjectIds: they leak creation time and expose the storage engine.

This is a **breaking** change, made once, before the portfolio front end ships against the API.

## Goals
1. **Success envelope** on every JSON route under `/admin/v1` and `/v1`:
   `{"status": "success", "message": "<short text>", "result": <payload>}`.
   - Lists: `result` is a page:
     `{"listOfItems": [...], "page", "pageSize", "total", "totalPages", "sortColumn", "sortOrder"}`.
   - Deletes answer `200` with `result: {"id": "<uuid>"}` instead of `204`.
   - `?stream=false` chat answers are enveloped; SSE streams, the resume file, `/healthz`, `/readyz`
     and `/metrics` are not (they are not JSON documents, or probes read by tools).
2. **Error envelope** with a code catalog in `app/exceptions/codes.py`:
   `{"status": "error", "message": "<text>", "result": null, "error": {"code", "requestId", "details?"}}`.
   The catalog gives each code its HTTP status and a default message; the OpenAPI description lists it.
   Renamed codes:

   | Old | New | Status |
   |---|---|---|
   | `VALIDATION_ERROR` | `INVALID_INPUT` | 422 |
   | `AUTH_REQUIRED` | `UNAUTHENTICATED` | 401 |
   | `AUTH_INVALID` | `INVALID_API_KEY` | 401 |
   | `DEPENDENCY_UNAVAILABLE` | `SERVICE_UNAVAILABLE` | 503 |
   | (405 mapped to `NOT_FOUND`) | `METHOD_NOT_ALLOWED` | 405 |

   Unchanged: `FORBIDDEN`, `NOT_FOUND`, `CONFLICT`, `PAYLOAD_TOO_LARGE`, `RATE_LIMITED`,
   `MODEL_UNAVAILABLE`, `STREAM_INTERRUPTED`, `INTERNAL_ERROR`. SSE `error` events use the same codes.
3. **Paging, sorting, search and filter** on every list route, with the same query parameters:
   `page` (1-based, default 1), `pageSize` (1–200, default 20), `sortColumn` (per-route allowlist),
   `sortOrder` (`asc` | `desc`, default `desc`), `q` (case-insensitive text search over named fields) and
   `filter` (`field:value` pairs, comma-separated, per-route allowlist, e.g. `filter=status:new,triage:hot`).
   An unknown sort column, filter field or filter value is `422 INVALID_INPUT`. The old `limit`, `status`,
   `kind`, `triage` and `severity` list parameters are replaced by `filter`; `since` stays on security
   events. List routes: conversations, leads, memories, records, review queue, security events, model
   overrides.
4. **camelCase JSON** for request and response bodies and query parameters: API models share one base
   (`alias_generator=to_camel`, `populate_by_name=True`), so snake_case input is still accepted. SSE event
   data keys are camelCase too. Free-form maps (`details`, `extras`, `redactionReport` keys) are data and
   keep their keys. Storage, audit details and model prompts stay snake_case.
5. **UUID v4 public ids**. Sources, documents, records, leads, owner memories and security events get a
   `uid` (UUID v4, unique) beside the MongoDB `_id`. The API only shows and accepts `uid`s, including
   references (`sourceId`, `documentIds`, `resumeDocumentId`). Path ids are parsed as UUIDs (anything else
   is `404`). Migration 0007 backfills `uid` (and `source_uid`, `resume_document_uid`); vault export and
   import keep them, and an import of an older export gets them added.
   Conversations keep their existing id (already a random UUID v4, as 32 hex characters); model overrides
   keep their natural key.
6. **Clients follow**: `make chat` / `make memory` (`scripts/chat.py`, `scripts/memory.py`), the Postman
   generator and their tests read the new shapes.

## Non-goals
- Database-side paging (`skip`/`limit`/`count` in MongoDB). Lists page in the API over the newest
  `LIST_SCAN_MAX` (1,000) rows that match the filters pushed down to the store; `total` counts those.
  Enough for one owner's vault; a later change can push paging into the stores.
- Chat `sources` / `citations` document ids: they come from vector payloads and stay internal ids in this
  change (follow-up: map them to `uid` in the live-document check).
- Versioned coexistence (`/v2` beside `/v1`): the front end is not live yet, so the contract changes in place.

## Acceptance criteria
1. Every JSON success under `/admin/v1` and `/v1` has `status: "success"`, a `message` and a `result`.
2. Every error under those prefixes (app errors, validation, unknown route, wrong method, unhandled) has
   `status: "error"`, `message`, `result: null` and `error.code` from the catalog, plus `error.requestId`.
3. No old code name (`VALIDATION_ERROR`, `AUTH_REQUIRED`, `AUTH_INVALID`, `DEPENDENCY_UNAVAILABLE`)
   remains in `app/` or `scripts/`.
4. Each list route accepts `page`, `pageSize`, `sortColumn`, `sortOrder`, `q`, `filter` and returns a page;
   `totalPages` is `ceil(total / pageSize)`; bad sort/filter input is `422 INVALID_INPUT`.
5. Response keys are camelCase; request bodies accept camelCase and snake_case.
6. Ids in responses are UUID v4; an ObjectId in a path is `404`; migration 0007 is idempotent and has
   `down`; export → import round-trips `uid`.
7. `make chat`, `make chat-list`, `make chat-continue`, `make memory` work against the new API; the
   Postman collection is regenerated.
8. `make check` passes.
