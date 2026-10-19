# Spec: public-agent

Status: draft, awaiting approval · Owner: Surya Prakash Giri · Ticket: none (personal repo; `tasks.md` is the task list)

## Problem
The portfolio needs an assistant that strangers can talk to. Visitors are untrusted: some will try prompt
injection, ask what model or database the assistant runs on, request poems or code, or ask it to do things.
The assistant must:
- answer only from published public facts, in the owner's first-person voice, clearly labelled as an AI twin
- serve the resume
- refuse everything else politely
- never leak private data, its own prompt or its infrastructure, even if a guard upstream misses something

## Goals
1. **Public routes**, mounted in both profiles; in the `public` profile they are the only routes besides health.
   - `POST /v1/chat` (scope `public:read`): body `{conversation_id?, message}`. Returns SSE (`meta`, `token`,
     `sources`, `done`, `error`) or `?stream=false` JSON.
   - `GET /v1/resume` (scope `public:read`): the current resume PDF.
   - `GET /v1/assistant` (scope `public:read`): greeting, AI-twin disclosure line, privacy note, availability
     fact.
2. **Visitor identity and rate limits.**
   - The portfolio's server route holds the service key and forwards the visitor address in `X-Visitor-Address`.
     The API trusts that header only from a key with `public:read`, and stores only the salted hash.
   - Redis limits per visitor: 8 messages/min and 60/day. A global cap of `PUBLIC_DAILY_MESSAGE_CAP` (default
     1500) protects the free model quotas.
   - Over a limit → 429 `RATE_LIMITED` with `Retry-After`.
3. **Input guard** (runs first, in this order; a single failure stops the request):
   1. Length cap (`PUBLIC_MAX_MESSAGE_CHARS`, default 800).
   2. NFKC normalisation; zero-width and control characters stripped.
   3. Heuristic injection rules: instruction override, role play as system, "print/reveal your prompt",
      encoded payloads.
   4. Llama Prompt Guard 2 (22M) through a new orchestrator role `guard`. It is Groq-hosted, and the input is
      public-tier visitor text. A score above `PROMPT_GUARD_THRESHOLD` blocks.
   5. With `FF_JEV_ENABLED`, Jev gives a second opinion on borderline scores.
   - A blocked message gets a fixed polite reply and is recorded as a security event. It never reaches retrieval
     or the generator.
4. **Scope router** (classifier role through `FlaggedClassifier`, tier public). Labels: `about_owner`,
   `opportunity`, `resume`, `availability`, `greeting`, `meta_probe`, `out_of_scope`, `action_request`.
   - `meta_probe`: questions about the assistant's own model, prompt, tools, data stores, architecture or hosting.
   - `out_of_scope`: poems, code, homework, general knowledge, opinions about others.
   - `action_request`: asks it to send, change, book, delete or log in to anything.
   - Each of these three gets a fixed reply in the owner's voice, with no retrieval and no generation.
   - `resume` and `availability` are answered from profile facts with a fixed template.
   - `opportunity` gets a fixed reply pointing to the contact form; the conversational lead capture arrives in
     the opportunities feature.
   - Questions about which technologies the owner used on a project are `about_owner`, not `meta_probe`.
5. **Retrieval:** the public graph builds its retriever with the collection list fixed to `kb_public` in code.
   No parameter, header or prompt can widen it. Same hybrid + rerank as the owner agent; weak or empty
   context → "I don't have that in my public profile — you can reach me through the contact form".
6. **Generation:** generator role at tier public. The system prompt carries:
   - first-person voice, disclosed as an AI twin
   - answer only from the numbered context and cite it
   - no claims beyond the context
   - no discussion of its own construction
   - stay brief
   Persona voice tuning comes later (persona-evals); this ships a plain, correct baseline.
7. **Output guard** (runs on the full answer before anything is sent; fail-closed for 1–3):
   1. **Canary scan:** any token matching the canary format whose SHA-256 is in the canary hash set (synced from
      the vault, hashes only) → block + critical event.
   2. **Identifier scan:** the rule-based recognisers from ingestion redaction (PAN, Aadhaar, UAN, IFSC,
      account, passport, DOB, roll numbers), moved into a light module with no Presidio dependency. Published
      contact details are allowlisted.
   3. **Disclosure scan:** self-referential statements about its model/provider/prompt/stack, and n-gram overlap
      with the system prompt above a threshold (prompt leak).
   4. **Grounding check** (judge role, tier public; Jev `noul` when flagged): "is every claim supported by the
      cited context?". Fail-open: on judge failure the answer is sent and the event is logged. A `false` verdict
      replaces the answer with the safe fallback.
   - Any block replaces the answer with the safe fallback and records a security event with the rule name only.
8. **Delivery:** the answer is buffered and checked, then sent. Over SSE it goes out as `token` events after the
   guard passes; no unguarded token is ever sent.
9. **Answer cache:** single-turn questions are cached in Redis by normalised-question hash plus
   `kb_public_version`. Publish and unpublish bump that version. TTL `PUBLIC_ANSWER_CACHE_TTL` (default 6 h).
   Guarded answers only.
10. **Conversations:** channel `public`, visitor hash, 24 h Redis window, 90-day TTL in MongoDB (existing). The
    message metadata stores the intent, guard verdicts (rule names), cache hit, model and latency.
11. **Resume:** `ResumeProvider`. In the vault it streams the profile's resume blob. In the public profile it
    serves `PUBLIC_RESUME_PATH`, which deploy-public provisions. 404 `NOT_FOUND` when none is set. Downloads
    are counted (no visitor data beyond the hash).
12. **Security events:** a `SecurityEvent` is written for every injection block, meta-probe, output-guard block,
    canary hit (`critical`), rate-limit burst and auth failure on public routes. Emailing them is
    observability-alerts.

## Non-goals
Conversational lead capture and email (opportunities), alert emails and Langfuse (observability-alerts),
persona voice and the red-team eval with real models (persona-evals), cloud deployment and canary-hash sync job
(deploy-public; this feature defines the sync format and a local loader), portfolio widget.

## Acceptance criteria
1. The public graph cannot query `kb_private` or `kb_confidential`. A test patches the retriever factory and
   asserts the collection list, and an integration test with a private-only fact returns the fallback.
2. In the `public` profile, no `/admin` route exists (404) and `/v1/*` needs `public:read`. `leads:write` alone
   can't chat.
3. Rate limits: the 9th message in a minute from one visitor gets 429 with `Retry-After`. Different visitors are
   independent. The global cap applies.
4. Each input-guard layer blocks its fixture set (length, control characters, heuristic patterns, guard-model
   score above the threshold via a fake). Blocked messages never call the router, retriever or generator.
5. Router fixtures: `meta_probe`, `out_of_scope` and `action_request` messages get fixed replies with zero
   retrieval or generation calls. "Which database did you use in the observability project?" routes to
   `about_owner`.
6. Output guard: a planted canary marker, a fake PAN, a self-disclosure sentence and a prompt-echo each cause a
   block plus a security event, and the visitor sees only the fallback. The allowlisted email passes.
7. A grounding `false` replaces the answer; a judge outage sends the answer and logs a fail-open event.
8. No `token` event is emitted before the output guard passes (asserted on event order with a slow fake guard).
9. A cache hit skips generation. Publish bumps the version, so the next ask misses.
10. `GET /v1/resume` returns the configured PDF with the right content type, or 404.
11. No visitor message or answer text appears in logs at INFO or above; security events store rule names, not
    the matched text.
12. `make check` passes. The deterministic red-team fixtures in `tests/redteam/` run in CI with fake models.

## Open questions
None.
