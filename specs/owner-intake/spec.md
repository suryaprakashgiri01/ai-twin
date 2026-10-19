# Spec: owner-intake

Status: draft, awaiting approval · Owner: Surya Prakash Giri · Ticket: none (personal repo; `tasks.md` is the task list)

## Problem
Adding knowledge to the vault means hand-writing JSON for the admin API: one call per note, record, URL or
profile change, plus follow-up calls to review and publish. The owner wants to describe things in plain
language instead ("had my second round with Acme yesterday, system design, went well"). The assistant should:
- ask for whatever is missing
- confirm before writing anything
- record it
- stay in the same conversation for follow-ups ("also add the HR round on Friday", "publish that note")
- suggest next steps, but act only with the owner's explicit yes

It runs on the owner's machine (this Mac or the Windows laptop) with the local model only.

## Goals
1. **One shared service layer, `AdminOps`**, extracted from the admin routes with no behaviour change. Ingest
   text/URL/file/GitHub, records CRUD, profile, review list, approve, publish, unpublish, relabel and delete.
   The routes and the assistant's tools both call it, so validation, audit and errors are identical. The
   assistant never calls the HTTP API.
2. **Two agents in one LangGraph graph, run in sequence:**
   1. **Intake (requirements gatherer)** reads the conversation and produces an `IntakeDraft`: the action, the
      payload collected so far, and a reply. Code validates the payload against the action's existing pydantic
      model (`TextIn`, `RecordIn`, `ProfileIn`, …) and works out the missing required fields. The model never
      decides on its own that a draft is complete. Missing fields → the assistant asks only for those and the
      turn ends.
   2. **Confirmation:** a complete draft is shown as a readable summary plus its JSON, and the graph pauses
      (LangGraph `interrupt`). The owner's next message is read as `yes` / `no` / an edit: rules first, model
      fallback. An edit goes back to intake with the change applied.
   3. **Executor** calls the matching `AdminOps` method and reports the outcome: ids, job status, proposed tier
      and the reason for it.
   - Then it **suggests** one next step from deterministic rules:
     - an ingested document that is public-suggested and still pending → "approve and publish it?"
     - a restricted document → "kept as a file only"
     - a record → "anything else from this round?"
   - It never acts on a suggestion without a new yes.
3. **Actions (v1):** `add_note` (interview experience, issue solved, feature built, …; any category),
   `ingest_url`, `ingest_file`, `ingest_github`, `add_record`, `update_record`, `delete_record`,
   `update_profile`, `show_review`, `source_status`, `approve`, `publish`, `unpublish`, `relabel`,
   `delete_document`, and `answer`.
   - `ingest_file` reads only from the inbox folder, `INTAKE_INBOX_DIR` (default `vault/inbox/`), by file name.
     It refuses paths outside it, including `..` and symlinks.
   - `answer` hands the question to the existing owner chat graph, so "how many interviews this month?" works
     in the same conversation.
4. **Safety:**
   - Every write needs a confirmation, and **delete actions need a second confirmation** (the owner retypes the
     document title or record company).
   - `publish` additionally requires the document to be approved and public, as today. The assistant can't
     bypass `AdminOps`.
   - The whole graph runs at tier `confidential`, so only local models (Ollama, or the mock in tests) are
     eligible. With no local model it refuses: "start Ollama: make ollama ARGS=start".
   - Audit actor: `agent:intake via key:<name>`.
   - No conversation text in logs at INFO+.
5. **Dates and inbox:** natural dates ("yesterday", "last Friday", "12 Oct") are normalised by code
   (`dateparser`) in `OWNER_TIMEZONE` (default `Asia/Kolkata`). The draft summary always shows the resolved
   date for the owner to check.
6. **State:**
   - Conversations reuse `ConversationService` (Redis/Valkey first, MongoDB after TTL) and the naming from
     conversation-cli, and gain `mode: ask | intake`.
   - The pending draft and the interrupt live in a MongoDB LangGraph checkpointer keyed by conversation id, so a
     confirmation survives an API restart and works across machines after a vault export/import.
   - Checkpoints of finished drafts older than 30 days are pruned.
7. **Interfaces (vault profile only):**
   - `POST /admin/v1/assistant` `{conversation_id?, message}` → SSE: `meta`, `token`, `draft` (when a summary
     awaits confirmation), `result` (after a write), `done`, or `error`.
   - CLI: `make twin` (Windows `./scripts/dev.ps1 twin`) starts an intake conversation.
   - `make chat-list` shows a MODE column; `make chat-continue` resumes either mode, at the pending confirmation
     if one exists.

## Non-goals
Public-side changes, voice input, bulk import, Google Drive OAuth, cloud models for intake, edits to documents'
text after ingestion, scheduling or reminders.

## Acceptance criteria
1. Routes keep their exact responses and audit entries after the `AdminOps` extraction (existing route tests pass
   unchanged).
2. "Add an interview with Acme" → the assistant asks for the missing required fields (role, stage, date) only.
   Once given, it shows the draft and pauses. No record exists until "yes". After "yes" exactly one record exists,
   with actor `agent:intake via key:<name>`.
3. "No" discards the draft. "Change the stage to round 3" updates the draft and re-asks for confirmation.
4. `delete_document` / `delete_record` need the second confirmation. A wrong retyped title cancels.
5. `ingest_file` accepts `resume.pdf` from the inbox and refuses `../.env`, absolute paths and symlinks out of the
   inbox, all without reading the file.
6. After ingest, the result reports the source id, status and proposed tier. For a public-suggested pending
   document it suggests approve-and-publish and does nothing until a new "yes".
7. A pending confirmation survives an API restart and a Redis flush: `continue` shows the draft again and "yes"
   executes it once.
8. With only cloud models enabled, the assistant refuses with the Ollama hint and makes no model call.
9. "How many interviews in October?" in an intake conversation is answered via the owner chat graph with the exact
   count.
10. "Yesterday" resolves to the right calendar date in `OWNER_TIMEZONE` and is shown in the draft.
11. No conversation text or draft payload appears in logs at INFO+.
12. `make check` passes. Scripted multi-turn conversations with the mock model cover AC 2–10; a live macOS run
    with Ollama covers 2, 6, 7 and 9.

## Open questions
None blocking. The default `OWNER_TIMEZONE` is `Asia/Kolkata`; change it in `.env`.
