# Spec: docs-architecture

Status: draft, awaiting approval · Owner: Surya Prakash Giri · Ticket: none (personal repo; `tasks.md` is the task list)

## Problem
The docs describe the target design in prose and one ASCII diagram. The code has moved on: three LangGraph
graphs (public, owner, intake), a guard stack, an ingestion pipeline and an orchestrator package that wraps
LangChain. A new reader cannot see how a request flows, where LangChain stops and custom code starts, or
what the trust boundaries are.

## Goals
1. Every diagram is Mermaid (renders on GitHub, diffs as text). No ASCII topology remains.
2. `README.md`: what the project is, quick start for macOS and Windows, a docs map.
3. `docs/hld.md` and `docs/threat-model.md` rewritten: tiers, trust boundaries, guards, request id and error
   envelope policy.
4. New `docs/components.md`, `docs/flows/public-flow.md`, `docs/flows/owner-flow.md` (owner and intake graphs)
   and `docs/rag-pipeline.md`, with flow and sequence diagrams that match `app/agents/graphs/`.
5. A LangChain section: what the project takes from LangChain (chat models, messages, callbacks, structured
   output) and where custom code takes over (selection, retries, fallbacks, guards, streaming).
6. `packages/llm-orchestrator/README.md` expanded: inventory, roles, `max_tier`, fallbacks, overrides,
   `structured_call`, budgets, Jev, hooks.

## Non-goals
- No code or config changes. No new behaviour.
- No personal data, employer data or real hostnames in any diagram.

## Acceptance criteria
1. `rg '```' docs README.md packages/llm-orchestrator/README.md` shows no fenced block that draws boxes with
   box-drawing characters; every diagram fence is `mermaid`.
2. Each graph diagram lists exactly the nodes and edges that `build_*_graph` adds (checked against the code).
3. Every relative link in the touched files resolves.
4. Names used in docs (roles, tiers, intents, routes, error codes) exist in code.
5. `make check` passes (docs only, so it must be unchanged).
