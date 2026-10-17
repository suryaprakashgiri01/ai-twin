# Tasks: public-agent

Each task is one commit (a large task may carry several). Commit subjects reference the task id.

| ID | Task | Verifies AC |
|---|---|---|
| T1 | Public routes skeleton, profile gating, visitor identity header, Redis rate limits | 2, 3 |
| T2 | `Role.GUARD` + Prompt Guard 2 inventory entry; input guard layers (+ Jev second opinion) | 4 |
| T3 | Scope router, fixed replies, profile-template answers for resume/availability | 5 |
| T4 | Light identifier module (moved from redaction), canary hash loader, output guard checks | 6, 7 |
| T5 | Public graph with fixed `kb_public` retriever, generation prompt, buffered SSE delivery | 1, 8 |
| T6 | Answer cache + `kb_public_version` bump on publish/unpublish | 9 |
| T7 | Resume provider + `/v1/resume`, `/v1/assistant` | 10 |
| T8 | Security event writer wired into every guard and auth failure; message guard metadata | 6, 11 |
| T9 | Red-team fixtures + tests, integration tests, real macOS E2E, dev-notes for macOS and Windows | 1–12 |
