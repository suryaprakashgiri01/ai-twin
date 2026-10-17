# Tasks: owner-agent

Each task is one commit (a large task may carry several). Commit subjects reference the task id.

| ID | Task | Verifies AC |
|---|---|---|
| T1 | Add LangGraph; hybrid retriever (prefetch + RRF, tier-scoped collections, filters, canary/deleted exclusion) | 1, 2 |
| T2 | Reranker + context builder (budget, citations, tier tracking) | 4, 5 |
| T3 | Typed read-only tools: `query_records`, `get_profile` | 3 |
| T4 | Owner graph: route (rewrite + decide), retrieve, records, profile, generate with tier-aware selection and withholding | 3, 4, 5, 6 |
| T5 | Streaming: pre-first-token fallback, SSE encoding, incomplete turns | 7, 8 |
| T6 | Owner chat service + routes (chat, conversations), message metadata | 7, 9 |
| T7 | Tests (mock model, fakes, Qdrant integration), real macOS E2E, dev-notes for macOS and Windows | 1–10 |
