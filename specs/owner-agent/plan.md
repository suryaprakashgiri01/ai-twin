# Plan: owner-agent

## Architecture
```
POST /admin/v1/chat ─▶ OwnerChatService
   load history (ConversationService) ─▶ owner graph:
      route (router role, structured) ─┬─ kb_search ─▶ Retriever (Qdrant hybrid RRF) ─▶ Reranker ─┐
                                       ├─ records ─▶ RecordsTool (typed aggregation) ──────────────┤
                                       ├─ profile ─▶ ProfileTool ───────────────────────────────────┤
                                       └─ chat ──────────────────────────────────────────────────────┤
                                                                       generate (tier = max used) ◀─┘
   stream tokens as SSE ─▶ finalize: append turns with metadata
```

## Module layout

| Module | Responsibility |
|---|---|
| `app/kb/retrieval/retriever.py` | Hybrid query over allowed collections, filters, canary/deleted exclusion |
| `app/kb/retrieval/rerank.py` | fastembed cross-encoder, score threshold |
| `app/kb/retrieval/context.py` | Token-budgeted context with citation numbering and tier tracking |
| `app/agents/tools/records.py`, `profile.py` | Typed read-only tools |
| `app/agents/graphs/owner.py` | `OwnerState`, nodes, graph build |
| `app/agents/prompts/owner.py` | Route and generate prompts (versioned constants; Langfuse later) |
| `app/agents/streaming.py` | Pre-first-token fallback, SSE event encoding |
| `app/services/owner_chat.py` | Glue: history, graph run, persistence, metadata |
| `app/api/routes/admin_chat.py` | Chat + conversations routes |

## Key decisions
- **Structured routing over native tool calls.** Free and local models handle tool calling unevenly; one
  structured call keeps every inventory model usable and keeps the routing decision testable.
- **One retriever for both agents.** The public agent will call the same class with `max_tier=public`, so the
  isolation rule lives in one place.
- **Tier decided by the context actually used, not by what was retrieved.** Withholding confidential chunks
  lowers the answer tier, so a cloud model can still answer the rest.
- **No checkpointer yet.** `ConversationService` already persists turns. A second store would duplicate state;
  the checkpointer arrives with interrupts in opportunities.
- **SSE via `StreamingResponse`.** No extra dependency; the request-context middleware already handles streaming
  bodies.

## Data model changes
`Message` gains `route?`, `citations[]` (document ids), `incomplete` (bool), `withheld` (count). Additive, no
migration needed beyond defaults.

## Risks
- Reranker model download on first use: cached under `vault/models`, checked by `doctor`.
- Local models can be slow for long contexts: the context budget is configurable (`OWNER_CONTEXT_TOKENS`,
  default 3000).
- A small model can mis-route: on `kb_search` with zero hits, try `records` once if the question has
  count/date words; the route decision is logged.
