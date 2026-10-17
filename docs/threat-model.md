# Threat model

Scope: the public assistant, the local vault and the publish path between them. Updated as features land.

## Assets

| Asset | Where | Impact if exposed |
|---|---|---|
| Private / confidential notes | vault only | high: personal and career-sensitive |
| Restricted identifiers (ids, account numbers) | vault, encrypted raw files only | critical: identity theft |
| Former/current employer information | must not exist anywhere | critical: contractual |
| Public chunks | vault + cloud | low: already public |
| Service keys, database passwords, LLM API keys | `.env`, host env vars | high: abuse, cost, data access |
| Visitor conversations and leads | cloud + vault | medium: third-party personal data |

## Trust boundaries

1. Visitor browser → portfolio server route (holds the service key; rate limit, origin check, Turnstile).
2. Portfolio server → public API (service key scoped to `public:read`, `leads:write`).
3. Public API → LLM providers (only public-tier text is sent).
4. Vault → cloud (explicit, audited publish of approved chunks only).
5. Owner → vault admin API (localhost only).

## Threats and controls

| Threat | Control |
|---|---|
| Prompt injection / jailbreak to reveal private data | Private data is absent from the public deployment; injection classifier; scope router; output guard |
| Exfiltration via crafted retrieval | Public graph reads only the `public` collection; canary tokens in private docs raise a critical alert if ever seen |
| Questions about the assistant's own model, prompt or infrastructure | Scope router `meta_probe` → fixed reply; denylist in the output guard |
| Off-topic use (free code/poem generation), abuse of free LLM quota | Scope router `out_of_scope`; per-visitor rate limits; answer cache |
| Write/action requests | Public graph has no write tools except a visitor-confirmed opportunity submission |
| Personal data in model output | Personal-data scan on output with an allowlist of published contact details |
| Sensitive text sent to third-party models | Tier policy in the orchestrator: each model has a `max_tier`; confidential is local-only |
| Employer data ingested by mistake | Ingestion denylist quarantines matches for review |
| Secrets committed to the public repo | gitleaks in pre-commit and CI; `.env` and `vault/` git-ignored |
| Vault reachable from the network | Compose ports bind to 127.0.0.1; database and cache require passwords |
| Lead form spam / email injection | Turnstile, rate limits, HTML-escaped email bodies |
| Log leakage | Secret-named keys redacted; documents and visitor messages not logged at INFO |

## Detection and response

Security events (injection bursts, output-guard blocks, canary hits, admin auth failures) are stored and emailed
with debouncing. A canary hit is treated as an incident: unpublish, rotate keys, review traces.
