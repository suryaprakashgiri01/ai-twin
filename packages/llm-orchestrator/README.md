# llm-orchestrator

Picks a chat model per call from a role chain, filtered by data tier, then runs it with retries and
fallbacks. It has no dependency on the ai-twin app. See `specs/llm-orchestrator/spec.md`.

Tiers are ordered `public < private < confidential < restricted`. A model serves a request only when
`request.tier <= model.max_tier`, and no model may declare `restricted`.
