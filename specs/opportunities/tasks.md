# Tasks: opportunities

Each task is one commit (a large task may carry several). Commit subjects reference the task id.

| ID | Task | Verifies AC |
|---|---|---|
| T1 | Lead model fields + migration 0005; `LeadDraft`/`LeadIn` validation | 2 |
| T2 | Email rendering + SMTP client + outbox worker (retries, failed state, suppressed digest) | 3, 4, 7, 8 |
| T3 | Lead service: caps, dedupe, Turnstile, triage + fit note | 5, 6, 7 |
| T4 | Capture loop in the public graph with interrupt/confirm via `MongoCheckpointSaver` (public ns, 24 h prune) | 1, 2, 9, 12 |
| T5 | `POST /v1/opportunities`, admin leads routes (audited), intake `show_leads`, `.env.example` SMTP/Turnstile keys | 10, 11 |
| T6 | Tests (scripted conversations, fake SMTP/Turnstile, abuse), live run, dev-notes for macOS and Windows | 1–13 |
