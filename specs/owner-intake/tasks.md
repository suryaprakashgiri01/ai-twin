# Tasks: owner-intake

Each task is one commit (a large task may carry several). Commit subjects reference the task id.

| ID | Task | Verifies AC |
|---|---|---|
| T1 | Extract `AdminOps` from admin routes (no behaviour change; actor-aware audit) | 1 |
| T2 | MongoDB checkpointer + pruning; `Conversation.mode` + migration 0004 | 7 |
| T3 | Action catalog, drafts (missing fields, date normalisation, inbox resolution), confirmation parsing, suggestions | 2–6, 10 |
| T4 | Intake graph: gather → confirm (interrupt) → second confirm → execute → report; `answer` via owner graph; local-only guard | 2–9 |
| T5 | `POST /admin/v1/assistant` (SSE) + intake service; CLI `twin`, MODE column, resume at pending draft; Makefile/dev.ps1 | 7, 11 |
| T6 | Tests (scripted conversations with mock model), live macOS E2E with Ollama, dev-notes for macOS and Windows | 1–12 |
