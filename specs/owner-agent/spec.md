# Spec: owner-agent

Status: draft, awaiting approval · Owner: Surya Prakash Giri · Ticket: none (personal repo; `tasks.md` is the task list)

## Problem
The vault now holds labelled, embedded knowledge, but the owner can't ask it anything yet. Owner mode needs a
conversational agent that:
- searches every tier the owner allows
- answers counting questions ("how many applications this week?") from structured records rather than
  guessing from text
- cites its sources
- remembers the conversation
- never sends confidential content to a cloud model

## Goals
1. **Hybrid retrieval service** (shared with the public agent later):
   - Dense + sparse queries fused with Qdrant's Query API (prefetch + RRF), run across the collections allowed by
     `max_tier`: `kb_public`, `kb_private`, `kb_confidential`.
   - Filters: category, tags, date range. Deleted documents and canary chunks are always excluded.
   - A local cross-encoder reranker (fastembed) narrows to `top_k` (default 6). A score threshold drops weak hits.
   - Results carry the chunk text, tier, document id, title, category and a citation index.
2. **Structured tools, read-only and typed:**
   - `query_records(kind, company?, stage?, date_from?, date_to?, group_by?)`: counts and lists via aggregation.
     The model fills a pydantic schema; no free-form queries.
   - `get_profile()`: availability, notice period, preferred roles, locations.
3. **Owner graph (LangGraph `StateGraph`):**
   - `route`: a structured-output call that rewrites the question using recent history and chooses
     `kb_search | records | profile | chat`. It runs on the `router` role at the tier of the history it reads.
   - `retrieve`: KB search, then rerank.
   - `records` and `profile`: the typed tools above.
   - `generate`: answer grounded in the gathered context with numbered citations; "I don't have that in the
     vault" when the context is empty.
   - `finalize`: persists the turn and its metadata.
   Routing uses structured output, not native tool calling, so every model in the inventory (including Ollama
   and mock) can drive it.
4. **Tier-aware generation:**
   - The answer tier is the highest tier among the context actually used. The generator is selected at that tier,
     so confidential context forces a local model.
   - If no model is eligible for that tier, confidential chunks are dropped, the answer is generated at the next
     tier down, and the response says how many sources were withheld and why.
5. **Memory:** the existing `ConversationService` (Redis cache-aside + MongoDB) is the single source of
   conversation history. The graph loads the last N turns at start and appends user and assistant turns at the
   end. Each turn records route, max tier, model, citations (document ids), latency and token counts.
6. **API (vault profile, `admin` scope):**
   - `POST /admin/v1/chat`: body `{conversation_id?, message, max_tier?=confidential, filters?, model?,
     prefer?}`. Streams Server-Sent Events by default (`meta`, `token`, `sources`, `done`, `error`);
     `?stream=false` returns one JSON body.
   - `GET /admin/v1/conversations` (owner channel, newest first) and `GET /admin/v1/conversations/{id}`.
7. **Streaming with fallback:** a model that fails before its first token falls back to the next eligible model.
   A failure after streaming has started ends with an `error` event that names the code, and the partial answer
   is saved and marked incomplete.
8. **Logging and privacy:** one structured line per graph step (route, counts, tiers, model, latency). No
   question, context or answer text at INFO or above.

## Non-goals
The public agent and guardrails (public-agent), persona voice (persona-evals), Langfuse tracing
(observability-alerts), write tools of any kind, the LangGraph checkpointer (deferred to opportunities, where
visitor-confirmation interrupts need it; owner memory is covered by `ConversationService`), the semantic cache.

## Acceptance criteria
1. Retrieval with `max_tier=private` never queries `kb_confidential`. Canary and deleted chunks never appear.
2. RRF fusion returns a document that only the sparse query matches (exact term) and one that only the dense
   query matches (paraphrase).
3. A records question ("how many interviews in September?") is answered from `query_records` with the exact
   count, and the KB is not searched.
4. With confidential context and only cloud models enabled, the answer is generated without the confidential
   chunks and reports them as withheld. With the mock or Ollama marked local, they are used and `done.tier` is
   `confidential`.
5. An empty context produces the "not in the vault" answer, not an invented one.
6. A follow-up question ("and the one before that?") is rewritten using history; the rewritten query is in the
   step log metadata, not its text.
7. SSE emits `meta` first and `done` last; `sources` lists the citation document ids. `stream=false` returns the
   same fields.
8. A pre-first-token failure falls back. A mid-stream failure ends with `error` and the turn is stored with
   `incomplete=true`.
9. History survives a Redis flush (loaded from MongoDB).
10. `make check` passes. Graph tests use the mock model, a fake embedder and fake retrieval; Qdrant hybrid search
    is an `integration` test on fake-persona fixtures.

## Open questions
None.
