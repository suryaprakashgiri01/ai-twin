# Tasks: conversation-cli

Each task is one commit. Commit subjects reference the task id.

| ID | Task | Verifies AC |
|---|---|---|
| T1 | Naming helpers + `Conversation.name/description` + title on first question + backfill migration | 1, 2 |
| T2 | Summaries with name/description; `by-name` and `PATCH` routes (audited) | 3 |
| T3 | `scripts/chat.py` (list, new, continue, rename, SSE client, resolution) + Makefile/dev.ps1 + `.env.example` | 4, 6 |
| T4 | Tests, live E2E (incl. Redis expiry), dev-notes for macOS and Windows | 1–8 |
