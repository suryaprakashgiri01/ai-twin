# Spec: llm-orchestrator

Status: draft, awaiting approval · Owner: Surya Prakash Giri · Ticket: none (personal repo; `tasks.md` is the task list)

## Problem
Every later feature calls a model: the ingestion labeller, both agent graphs, the scope router, the guards and
the evaluators. Free tiers are rate-limited and come and go, and models differ in what data they may see. Only
public text may leave the house, and confidential text must stay on a local model. Without one layer that
owns model choice, each feature would hard-code a provider and re-implement retries, fallbacks and the tier
rule. Sooner or later one of them would send private text to the wrong place.

## Goals
1. A reusable SDK package, `packages/llm-orchestrator` (import `llm_orchestrator`), in the repo's uv workspace.
   It has no dependency on `app/`.
2. **Model inventory:** committed defaults in `config/models.yaml`, plus an override layer behind an
   `OverrideStore` protocol. F1 ships an in-memory store. kb-storage persists overrides in MongoDB. Wiring that store into the
   orchestrator is the first task of ingestion, once both features are merged. Each model declares
   provider, model name, roles, `max_tier`, context window, whether the provider may train on inputs, free-tier
   limits, cost, and `enabled`.
3. **Tier policy:** tiers are ordered `public < private < confidential < restricted`. A model may serve a request
   only if `request.tier <= model.max_tier`. No model may have `max_tier: restricted`, so restricted text never
   reaches any model. A model that may train on inputs is capped at `public`.
4. **Role-based selection:** each role (`router`, `classifier`, `generator`, `judge`) has an ordered chain:
   default first, then fallbacks. The chain is filtered by tier and `enabled` at call time.
5. **Dynamic override per call:** `model=<id>` or `prefer=fast|quality|local`. Overrides are honoured only for
   privileged callers. They are rejected with a typed error when the model is disabled, doesn't serve the role,
   or is below the request tier. A rejected override never silently falls back to another model.
6. **Resilience:** retry transient failures (timeout, 429, 5xx) with jittered backoff, then move to the next
   allowed model. Permanent failures (401/403/4xx) skip retries. The result reports the model used, attempts and
   whether a fallback was used.
7. **Structured output:** `structured(role, tier, schema)` returns a validated pydantic object. One repair attempt
   on parse failure, then the next model.
8. **Classifier protocol:** `classify(text, question, labels, tier)` → label, per-label probabilities,
   confidence, model.
   - `LLMClassifier`: structured output through the orchestrator.
   - `JevClassifier`: TypeSafe `POST /v1/systemone` with a `choice` question, `max_tier: public`.
   - `FlaggedClassifier`: uses Jev when `FF_JEV_ENABLED` is on and the text is public-tier; falls back to the LLM
     classifier when the flag is off, Jev errors, or the tier is above public.
9. **Observability hooks:** every call takes optional LangChain callbacks (Langfuse arrives in
   observability-alerts) and emits one structlog line per attempt: role, tier, model, outcome, latency, tokens.
   Prompt and response content is never logged.
10. **App wiring:** the app builds one orchestrator at startup from settings and the inventory file.
    `/readyz` reports how many models are configured per role (informational, not gating).

## Non-goals
Admin model routes and the MongoDB override store (kb-storage, which also brings admin auth), embeddings and
reranking (ingestion/retrieval), Langfuse integration (observability-alerts), agent graphs, budgets beyond
per-call `max_tokens`.

## Providers (initial inventory)

| Provider | Integration | Default `max_tier` | Notes |
|---|---|---|---|
| Groq | `langchain-groq` | private | primary for router, classifier, generator |
| Cerebras | `langchain-cerebras` | private | fallback |
| Gemini (free tier) | `langchain-google-genai` | public | free tier may train on inputs |
| OpenRouter `:free` | `langchain-openai` with base URL | public | upstream providers may log |
| Ollama (local) | `langchain-ollama` | confidential | only model allowed for confidential |
| Jev (TypeSafe) | `httpx` adapter, classifier only | public | behind `FF_JEV_ENABLED` |
| mock | `langchain-core` fake chat model | confidential | tests and keyless local runs |

Model ids are checked against each provider's model list during the build. `max_tier` defaults follow
provider data-use terms as read at build time, and every value is overridable.

## API (SDK)

```python
orch = Orchestrator.from_config(inventory, settings, override_store)
result = await orch.chat(role=Role.GENERATOR, tier=Tier.PRIVATE, messages=[...], prefer="quality")
result.text, result.model_id, result.attempts, result.fallback_used
obj = await orch.structured(role=Role.ROUTER, tier=Tier.PUBLIC, schema=RouteDecision, messages=[...])
cls = await classifier.classify(text, question="Which intent?", labels={"about_owner": "...", ...}, tier=Tier.PUBLIC)
```

Errors: `NoEligibleModelError` (nothing allowed for role and tier), `OverrideRejectedError` (with a reason),
`AllModelsFailedError` (with each attempt's reason).

## Configuration

New settings: `GROQ_API_KEY`, `CEREBRAS_API_KEY`, `GEMINI_API_KEY`, `OPENROUTER_API_KEY`, `OLLAMA_BASE_URL`,
`OLLAMA_MODEL`, `TYPESAFE_API_KEY`, `JEV_MODEL` (pinned, default `jev-1.13.0`), `LLM_TIMEOUT_SECONDS`,
`LLM_MAX_ATTEMPTS`, `MODEL_INVENTORY_PATH`. A model whose key is missing is treated as disabled, never as an
error.

## Acceptance criteria
1. Loading `config/models.yaml` rejects a model with `max_tier: restricted`, or one that may train on inputs
   with `max_tier` above public.
2. `chat` with `tier=confidential` only ever calls models with `max_tier >= confidential` (Ollama or mock). With
   none available it raises `NoEligibleModelError` and makes no network call.
3. A transient failure is retried up to `LLM_MAX_ATTEMPTS` and then falls back. A permanent failure falls back
   without retrying. The result reports model, attempts and `fallback_used`.
4. An override to a disabled, wrong-role or under-tier model raises `OverrideRejectedError`. An unprivileged
   caller's override is ignored, and the ignore is logged.
5. `structured` returns a valid schema instance, or tries the next model after one failed repair.
6. `FlaggedClassifier` calls Jev only when the flag is on and the tier is public. On a Jev error it returns the LLM
   classifier's answer. Jev requests match the documented `systemone` body, and the `choice`, `probabilities`
   and `confidence` fields are mapped.
7. Overrides from the `OverrideStore` (enable/disable, `max_tier` lowered, role chain reordered) take effect on
   the next call. Raising `max_tier` above the file value is refused.
8. No log line contains prompt or completion text. Tests assert this.
9. CI tests make no network calls; providers are faked.

## Open questions
None blocking. Jev access is waitlisted; until a key exists, the adapter is tested against recorded responses
only.
