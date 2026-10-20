# Plan: docs-architecture

## Approach
Read the code first, then write each document from it, not from the old docs. Graph diagrams are derived from
`build_public_graph`, `build_owner_graph` and `build_intake_graph` and from the `_after_*` routing functions.

## Files
| File | Change |
|---|---|
| `docs/hld.md` | Rewrite: purpose, principles, Mermaid topology, tiers, storage, observability, delivery |
| `docs/threat-model.md` | Rewrite: assets, tiers, trust boundaries diagram, guards, id and envelope policy |
| `docs/components.md` | New: component map, package and module responsibilities, LangChain section |
| `docs/flows/public-flow.md` | New: public graph, lead capture, delivery sequence |
| `docs/flows/owner-flow.md` | New: owner graph, intake graph, confirmation sequences |
| `docs/rag-pipeline.md` | New: ingestion, publish, retrieval, context building |
| `packages/llm-orchestrator/README.md` | Expand |
| `README.md` | Rewrite |

## Risks
- Docs drift from code: each flow doc names the code function it was derived from.
- Mermaid syntax errors: labels with punctuation are quoted.
