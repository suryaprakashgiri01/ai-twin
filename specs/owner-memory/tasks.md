# Tasks: owner-memory

Each task is one commit (a large task may carry several). Commit subjects reference the task id.

| ID | Task | Verifies AC |
|---|---|---|
| T1 | Data model: conversation summary fields, `owner_memories` document, migration 0006, vault export/import | 8 |
| T2 | Digester: one structured call (title, description, summary, candidates), periodic + on-exit triggers, tier rules | 1, 2 |
| T3 | Memory service: learn (mask, filter, dedupe, pending/bump, auto-accept switch), explicit add, accept/edit/archive/delete, audited | 4, 6, 7 |
| T4 | Context builder: preferences block + summary for long chats in owner chat and intake prompts; public agent untouched | 3, 5 |
| T5 | Interfaces: admin memory routes, intake `remember`/`forget`/`show_memory`, `make memory`, `/remember` + `/memory`, continue banner summary, exit notice | 6, 9 |
| T6 | Tests (scripted fixtures), live macOS run with Ollama, dev-notes + Postman | 1–11 |
