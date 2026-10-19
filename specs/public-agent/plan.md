# Plan: public-agent

## Architecture
```
portfolio server ──(service key, X-Visitor-Address)──▶ /v1/chat
  auth(public:read) ─▶ rate limit (Redis) ─▶ answer cache? ─hit─▶ send
  public graph:
    input_guard ──block──▶ fixed reply + SecurityEvent
      └▶ scope_router ──meta/oos/action──▶ fixed reply (+ event for meta)
            ├─ resume/availability ─▶ profile template
            └─ about_owner ─▶ retrieve(kb_public only) ─▶ generate(tier=public)
                                   ─▶ output_guard ──block──▶ fallback + SecurityEvent
                                   ─▶ buffered SSE ─▶ cache ─▶ persist turn
```

## Module layout

| Module | Responsibility |
|---|---|
| `app/guardrails/input.py` | Length, normalisation, heuristic rules, guard-model score, Jev second opinion |
| `app/guardrails/scope.py` | Intent labels, router call, fixed replies table |
| `app/guardrails/output.py` | Canary, identifier, disclosure, prompt-leak, grounding checks; verdict type |
| `app/guardrails/identifiers.py` | Rule recognisers moved out of ingestion redaction (no Presidio import) |
| `app/guardrails/canary_hashes.py` | Hash set loader (vault: from `canaries`; public: synced JSON) |
| `app/agents/graphs/public.py` | `PublicState`, nodes, graph build with fixed `kb_public` retriever |
| `app/agents/prompts/public.py` | System and grounding prompts |
| `app/services/public_chat.py` | Rate limit, cache, graph run, buffered delivery, persistence |
| `app/services/rate_limit.py` | Redis sliding windows (visitor minute/day, global day) |
| `app/services/answer_cache.py` | Normalised key + `kb_public_version` |
| `app/services/resume.py` | `ResumeProvider` (vault blob / public file) |
| `app/services/security_events.py` | Event writer with severity and rule names |
| `app/api/routes/public.py` | `/v1/chat`, `/v1/resume`, `/v1/assistant` |
| `packages/llm-orchestrator` | New `Role.GUARD`; inventory entry for Prompt Guard 2 |
| `tests/redteam/*.yaml` | Deterministic attack fixtures, grouped by layer |

## Key decisions
- **Isolation in code, not config.** The public graph never receives a tier or collection argument. A unit test
  pins it.
- **Buffer then guard then send.** Streaming before guarding would leak the very tokens the guard exists to
  stop. Groq latency keeps the buffered reply fast enough.
- **Fixed replies for refusals.** No model call, so no tokens are spent and nothing new can leak; the voice is
  tuned in persona-evals.
- **Fail-closed vs fail-open.** Leak checks fail closed. The grounding check is a quality check and fails open
  with an event, so a judge outage doesn't take the assistant down.
- **Canary hashes, not markers, leave the vault.** The public deployment can detect a leak without holding
  anything that identifies private documents.
- **Guard model via the orchestrator.** It gets the same inventory, fallback and tier rules, and stays swappable
  (for example to Jev) without code changes.

## Data model changes
`Message` gains `cache_hit` and `guards{input, scope, output}` (rule names), additive. `SecurityEvent` kinds are
extended. Redis keys: `rl:v:{hash}:{min|day}`, `rl:global:{day}`, `ac:{version}:{hash}`, `kb_public_version`.

## Risks
- **The guard model is too strict or too lenient on short questions.** The threshold is configurable, and
  persona-evals measures false positives on the golden set.
- **The disclosure scan clashes with legit skill answers** ("I used MongoDB"). It only matches
  self-referential patterns about the assistant, never technology names alone; the router fixtures cover this.
- **Groq quota exhaustion.** The global cap plus answer cache; inventory fallbacks to Cerebras/OpenRouter at the
  public tier.
