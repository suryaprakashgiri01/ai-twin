# Plan: llm-orchestrator

## Architecture
A uv workspace member, `packages/llm-orchestrator`, holds a provider-agnostic core. That core contains the
inventory, policy, selection, an executor with retries and fallbacks, and the classifiers. It sits on top of
LangChain chat models, so the agent graphs can also use the selected model directly as a `BaseChatModel`. The
app only builds and holds one instance.

```
call(role, tier, override?) ─▶ Selector ─▶ eligible chain [m1, m2, …]   (Policy: tier, enabled, role, key present)
                                   │
                                   ▼
                              Executor ── m1: attempt × N (tenacity, transient only) ──▶ ok → Result
                                   └──── m2 … on exhaustion / permanent error ─────────▶ AllModelsFailedError
```

## Module layout

| Module | Responsibility |
|---|---|
| `llm_orchestrator/types.py` | `Tier` (ordered IntEnum), `Role`, `ModelSpec`, `CallResult`, `Attempt`, errors |
| `llm_orchestrator/inventory.py` | Load and validate YAML; merge `OverrideStore` layer; reject unsafe tiers |
| `llm_orchestrator/policy.py` | `eligible(spec, role, tier)`; override validation with a reason |
| `llm_orchestrator/selection.py` | Role chains, `prefer` ordering (fast/quality/local tags), override handling |
| `llm_orchestrator/providers.py` | `build_chat_model(spec, secrets)` via LangChain integrations; mock provider |
| `llm_orchestrator/executor.py` | Retry/fallback loop, error classification, per-attempt structlog line |
| `llm_orchestrator/orchestrator.py` | Public facade: `chat`, `structured`, `chat_model_for` |
| `llm_orchestrator/classify/{base,llm,jev,flagged}.py` | Classifier protocol and the three implementations |
| `llm_orchestrator/overrides.py` | `OverrideStore` protocol + `InMemoryOverrideStore` |
| `config/models.yaml` | Default inventory and role chains |
| `app/core/llm.py` | Build the orchestrator from `Settings`; attach to `app.state` |

## Key decisions
- **LangChain models, our own loop.** `.with_fallbacks()` can't enforce a per-call tier filter, report attempts,
  or tell transient errors from permanent ones. A small executor over `ainvoke` keeps that explicit and
  testable. The selected model can still be handed to LangGraph nodes.
- **Tier as an ordered enum** makes the policy a single comparison, and it can't be bypassed by string typos.
- **Overrides can only tighten safety.** The file is the ceiling for `max_tier`, so a compromised admin key can't
  widen data exposure.
- **Jev through httpx, not its SDK.** It is one endpoint. We avoid the dependency and keep errors in our own
  taxonomy: 429/529 count as transient, 400/401/422 as permanent.
- **Missing key = disabled model.** The same inventory works on a laptop with no keys (mock + Ollama) and in the
  cloud.

## Data model
None persisted in this feature. `OverrideStore` keeps `{model_id: {enabled?, max_tier?}}` and
`{role: [model_id, ...]}` in memory. kb-storage persists them.

## Risks
- **Free-tier model ids change.** The ids live only in YAML, and `/readyz` shows per-role counts. A changed id
  shows up as a permanent error followed by a fallback.
- **LangChain integration package churn.** Pin minor versions and keep provider construction in
  `providers.py` only.
- **Provider data-use terms change.** `max_tier` can be overridden downward without a deploy.
