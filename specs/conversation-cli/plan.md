# Plan: conversation-cli

## Architecture
```
scripts/chat.py ──httpx (X-API-Key from .env)──▶ /admin/v1/conversations[/by-name|/{id}]   list / resolve / rename
                 └──────────────── SSE ───────────▶ /admin/v1/chat {conversation_id}       new / continue
OwnerChatService.prepare(new conversation) ─▶ naming.name_for(first question) ─▶ ConversationIndex.set_title (upsert)
history: ConversationService.history (Redis → MongoDB → refill)   (unchanged)
```

## Module layout

| Module | Responsibility |
|---|---|
| `app/services/naming.py` | `slugify_title`, `describe`, stop-word list, uniqueness suffix |
| `app/db/documents.py` | `Conversation.name`, `Conversation.description`, index on `channel`+`name` |
| `app/services/owner_chat.py` | Title on first question; summaries carry name/description; `by_name`, `update_title` |
| `app/api/routes/admin_chat.py` | `by-name` and `PATCH` routes |
| `app/migrations/m0003_conversation_names.py` | Backfill from the first user turn |
| `scripts/chat.py` | CLI: list, new, continue, rename; SSE client; name resolution |
| `Makefile`, `scripts/dev.ps1` | `chat`, `chat-list`, `chat-continue` |

## Key decisions
- **Deterministic names, not model titles.** Instant, testable, no Ollama dependency. `rename` covers taste.
- **The title is written with an upsert in `prepare`.** A new conversation may reach MongoDB only after its first
  turn persists in the background; the upsert's `$setOnInsert` matches the turn store's, so either can create
  the document.
- **The CLI talks HTTP to the running API.** One process owns the models and storage; the CLI stays a thin,
  dependency-light client (httpx only) that runs the same way on both machines.
- **Context comes from the server.** The CLI never resends history; it shows a recap for the human only.

## Data model changes
`Conversation` gains `name?` and `description?`. A non-unique index on `channel`+`name` serves lookups;
uniqueness is enforced in code (owner channel only, single writer).

## Risks
Two first questions arriving at once with the same slug could race on the suffix. That needs a single owner
typing in two terminals at the same instant; a `PATCH` fixes it.
