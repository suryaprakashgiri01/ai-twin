# Spec: owner-memory

Status: draft, awaiting approval · Owner: Surya Prakash Giri · Ticket: none (personal repo; `tasks.md` is the task list)

## Problem
The owner agents forget too much.
- Within a conversation, only the last `OWNER_HISTORY_TURNS` (8) turns reach the model, so a long chat loses
  its beginning.
- Across conversations nothing carries over. The owner repeats how they like answers, what they call things
  ("Magnus Eval" vs "magnus-eval") and standing instructions.
- The title refresh rebuilds its view of the conversation from scratch on every exit.

The owner wants:
- a rolling **conversation summary** that keeps long chats coherent and feeds titles
- a small, **editable** set of **owner preferences**, learned from conversations and injected into the owner
  agents

All of it is computed by the local model when the content requires, and never shared with the public agent.

## Goals
1. **Rolling conversation summary.**
   - Each owner conversation (ask and intake) keeps a `summary` of at most 1,200 characters, plus
     `summarized_turns`.
   - It's updated:
     - in the background every `SUMMARY_EVERY_TURNS` new turns (default 6)
     - on `/exit`, with the same model call as the title refresh
   - The model runs at the conversation's tier (intake and confidential → local only). With no eligible model
     nothing changes.
   - The summary states the topics, what was decided and what happened, using the real reply outcomes
     (`done`, `error`, `not found`) from metadata. A failed action is never written as done.
2. **The summary in context.**
   - When a conversation has more turns than `OWNER_HISTORY_TURNS`, the owner chat and intake prompts get
     "Earlier in this conversation: <summary>" ahead of the recent turns, within a token budget
     (`SUMMARY_CONTEXT_TOKENS`, default 300).
   - `make chat-continue` shows the summary in its banner.
3. **Owner memories.** A new `owner_memories` collection; each item is one short statement (≤ 200 chars) of
   one of three kinds:
   - `preference`: how the owner likes answers, e.g. "concise bullets with sources first".
   - `instruction`: a standing rule, e.g. "show dates as 12 Oct 2026".
   - `alias`: a name the owner uses, e.g. "magnus-eval = Magnus Eval project".

   Career facts are **not** memories: they go through intake into the vault, so there's one source of truth.
   Each item also stores `status` (`pending | active | archived`), the source conversation, `confidence`,
   created/updated times, `uses` and a `tier` (private, or confidential when learned from a confidential
   conversation).
4. **Learning memories.**
   - The `/exit` refresh (and the periodic summary) also returns memory *candidates*, extracted from the
     **owner's own messages only**.
   - Code then:
     - masks identifiers (the shared recognisers)
     - drops candidates over the length cap or of an unknown kind
     - de-duplicates by normalised text and embedding similarity (≥ 0.90 with the local fastembed model): a
       duplicate bumps `uses` and `updated_at`
   - New candidates are stored as **`pending`**. Nothing learned silently changes how the agents behave.
   - `MEMORY_AUTO_ACCEPT=true` (off by default) lets candidates with confidence ≥ 0.8 go straight to `active`.
5. **Explicit, editable memory.**
   - The owner can add one directly: `/remember <text>` in either CLI, or "remember that I prefer …" in
     `make twin`. These are `active` at once (the owner said it).
   - List, accept, edit, archive and delete through:
     - `make memory` (macOS) / `./scripts/dev.ps1 memory` (Windows): `list [--pending]`, `accept <id>`,
       `edit <id> "<text>"`, `forget <id>`, `add "<text>" [--kind …]`
     - `/memory` inside the chat loops
     - intake actions `remember`, `forget`, `show_memory`
     - admin API `GET/POST /admin/v1/memories`, `PATCH /admin/v1/memories/{id}`,
       `DELETE /admin/v1/memories/{id}`
   - Every change is audited.
   - The exit line says when there are new pending memories ("2 new memories to review: make memory").
6. **Memories in context.**
   - Active memories go into the owner chat and intake system prompts as an "Owner preferences" block: at most
     20 items, `MEMORY_CONTEXT_TOKENS` (default 400), ordered instructions → preferences → aliases → most
     used.
   - A confidential memory is injected only when the call's tier is confidential (local model).
   - **Never** in the public agent: the public persona is persona-evals' job, with its own explicitly approved
     settings.
7. **Privacy and portability.**
   - Memories and summaries live in the vault and are included in `make export` / `import`.
   - In Langfuse they're owner content, masked by default.
   - No conversation or memory text in logs at INFO+.

## Non-goals
Semantic search over past conversations ("what did we discuss last week about X"), which is v2. Memories for
visitors. Career facts outside the vault. Automatic deletion of stale memories (only `archive` by the owner).

## Acceptance criteria
1. After 6 new turns, a background refresh writes `summary` and `summarized_turns`. `/exit` refreshes summary
   and title in one model call. Intake and confidential conversations use only the local model; with none,
   nothing changes.
2. A summary never states a failed action as done (fixture with an `error` outcome).
3. A conversation with more turns than `OWNER_HISTORY_TURNS` sends the summary ahead of the recent turns,
   within the token budget. A short one doesn't.
4. Memory candidates come only from owner messages. Identifiers are masked, over-long or unknown-kind ones are
   dropped, a near-duplicate bumps the existing item, and new ones are `pending`.
5. Pending memories are never injected. Active ones are, in order and within the budget. A confidential memory
   is injected only on confidential (local) calls. The public agent never receives any memory (asserted on its
   prompt).
6. `/remember`, the intake `remember` action, `make memory add` and `POST /admin/v1/memories` create `active`
   items. accept / edit / forget work from the CLI, intake and API, and each writes an audit entry.
7. `MEMORY_AUTO_ACCEPT=true` activates only candidates with confidence ≥ 0.8.
8. `make export` → `import` round-trips memories and summaries.
9. `make chat-continue` shows the summary. The exit line reports new pending memories.
10. No memory or summary text in logs at INFO+. Langfuse traces of these calls are masked.
11. `make check` passes. Scripted model fixtures cover AC 1–7; a live macOS run with Ollama covers 1, 3, 4, 5
    and 9.

## Open questions
None blocking. Defaults: pending-first learning, 6-turn summary cadence, 20-item and 400-token memory budget.
All are configurable.
