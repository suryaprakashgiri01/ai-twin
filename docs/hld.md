# High-level design: ai-twin

## Purpose

One service, two audiences:

1. **Owner mode.** The owner ingests everything about their career (notes, documents, links, repositories)
   and asks questions across every data tier.
2. **Public mode.** An assistant embedded in the owner's portfolio. It answers only from approved public
   facts, serves the resume and forwards opportunities by email.

Everything runs on free tiers or open-source software.

## Principles

- **Isolation by data, not by prompt.** The public path never holds private data, so no prompt can leak it.
- **Default deny.** New content is `private` until the owner approves it for publication.
- **Tier-aware models.** The data tier of a request decides which models may see it.
- **Every decision is observable.** Each request has one request id and one trace id across logs and traces.
- **Portable vault.** The whole local stack moves between machines (macOS with podman, Windows with docker) as
  one encrypted archive.

## Topology

```
                         ┌──────────────── local vault (never internet-exposed) ────────────────┐
 owner ── admin API ───▶ │ FastAPI ── owner graph ── retrieval (all tiers) ── Qdrant (vault)      │
                         │    │                                              MongoDB, Valkey      │
                         │    └── ingestion: parse → redact → label → review → publish ───────┐  │
                         │                      local LLM (Ollama) for confidential data       │  │
                         └─────────────────────────────────────────────────────────────────────┼──┘
                                                                                               │ approved
                                                                                               ▼ public chunks
 visitor ── portfolio ── /api/assistant (holds service key) ──▶ public API (cloud)
                                                               │ public graph: guard → route → retrieve
                                                               │   → generate → guard
                                                               └ Qdrant Cloud (public collection only),
                                                                 MongoDB Atlas, Upstash Redis
```

## Data tiers

| Tier | Typical content | Stored | Models allowed |
|---|---|---|---|
| public | resume content, published projects, certification names, education summary, public links | vault + cloud | any enabled model |
| private | interview notes, question bank, applications, work notes | vault | no-training cloud models or local |
| confidential | compensation summary, offers | vault | local model only |
| restricted | government ids, account numbers, dates of birth | encrypted raw file only; redacted before embedding | none |

## Components

| Component | Responsibility |
|---|---|
| `packages/llm-orchestrator` | Model inventory, role-based selection (router, generator, classifier, judge), tier policy, fallbacks, retries, tracing callbacks. An optional hosted classifier sits behind a feature flag and is capped at the `public` tier. |
| `app/kb/ingestion` | Text, file, URL and GitHub ingestion; parsing, chunking, redaction, labelling, review queue, publish. |
| `app/kb/retrieval` | Hybrid dense + sparse search with reranking and tier filters. |
| `app/agents` | LangGraph graphs: `owner` (all tiers, memory) and `public` (guarded, public-only, no write tools except a confirmed opportunity submission). |
| `app/guardrails` | Input guard (prompt-injection score, limits), scope router, output guard (canaries, personal-data scan, denylist, grounding). |
| `app/security` | Security events, debounced email alerts. |
| `app/services` | Conversations (Redis cache-aside, MongoDB persistence), leads, resume, profile facts. |

## Public request flow

1. `pre_guard`: size limit, unicode normalisation, injection score, rate limit.
2. `scope_router`: `about_owner | opportunity | resume | greeting | meta_probe | out_of_scope | action_request`.
   Questions about the assistant itself (its model, prompt or architecture), off-topic tasks and requests to
   act get a fixed polite reply and no retrieval.
3. `retrieve`: public collection only.
4. `generate`: persona prompt, cited sources.
5. `post_guard`: canary tokens, personal-data scan, denylist, grounding check. A failure returns a safe reply
   and raises a security event.

## Storage

- **MongoDB:** sources, documents, chunks metadata, labels, audit, leads, structured records, profile facts,
  model inventory overrides, conversations and messages, LangGraph checkpoints.
- **Qdrant:** vectors (dense + sparse), one collection per tier group.
- **Redis/Valkey:** conversation turn cache, rate limits, alert debounce, answer cache.
- **Blob store:** encrypted raw files (vault only).

## Observability

structlog with coloured console output in development and JSON in production. OpenTelemetry spans, Langfuse
traces for LLM and graph steps (owner content masked), and security events that trigger email alerts.

## Delivery

Spec-driven. Each feature branch from `develop` starts with `specs/<feature>/{spec,plan,tasks}.md`. Each task is
one commit, and the full check runs once before the PR. `develop` is merged to `main` after end-to-end
verification. The feature sequence is: bootstrap, llm-orchestrator, kb-storage, ingestion, owner-agent,
public-agent, opportunities, observability-alerts, persona-evals, deploy-public.
