# ai-twin

A personal knowledge base and AI twin. One FastAPI + LangGraph service with two faces:

- **Owner mode** (local vault): ingest notes, files, links and repos; ask anything across every data tier.
- **Public mode** (portfolio assistant): answers only from approved public facts, serves the resume and
  forwards opportunities by email.

Private data never leaves the local vault; the public deployment only ever holds published public chunks.

## Quick start

Prerequisites: [uv](https://docs.astral.sh/uv/) and Docker or Podman with a compose provider.

| | macOS / Linux | Windows (PowerShell 7) |
|---|---|---|
| Check the machine | `make doctor` | `./scripts/dev.ps1 doctor` |
| First-time setup (`.env`, deps, hooks, stack) | `make setup` | `./scripts/dev.ps1 setup` |
| Run the API | `make run` | `./scripts/dev.ps1 run` |
| Stack status / logs | `make status` / `make logs` | `./scripts/dev.ps1 status` / `logs` |
| Quality gate | `make check` | `./scripts/dev.ps1 check` |
| Remove containers, data, caches | `make cleanup ARGS="--apply"` | `./scripts/dev.ps1 cleanup --apply` |

Then open http://127.0.0.1:8000/docs. `GET /healthz` is liveness; `GET /readyz` checks MongoDB, Redis (Valkey)
and Qdrant, and reports how many models each role can use (informational). Model providers are
configured in `.env` and `config/models.yaml`; a provider with no key is simply disabled.

## Docs

- [High-level design](docs/hld.md)
- [Threat model](docs/threat-model.md)
- Feature specs: [`specs/`](specs/)

## Stack (target; features land one by one)

FastAPI · LangGraph · LangChain · Langfuse · MongoDB · Valkey/Redis · Qdrant · structlog · OpenTelemetry · uv
