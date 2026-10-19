# Plan: bootstrap

## Architecture
A uv workspace root holds the `app` package. `packages/` stays empty until the llm-orchestrator feature adds
it. The core pieces are ported from a previous personal project and trimmed: settings, structlog logging,
request context, error envelope, request-context middleware. Health checks go through small checker objects,
so tests swap in fakes without a running stack.

## Module layout

| Module | Responsibility |
|---|---|
| `app/core/config.py` | `Settings` (pydantic-settings), feature flags prefixed `ff_`, `get_settings()` |
| `app/core/logging.py` | structlog + stdlib bridge, colour on TTY, JSON otherwise, secret redaction |
| `app/core/context.py` | request/session/user contextvars |
| `app/core/app_factory.py` | `create_app()`, lifespan, middleware, handlers, routers |
| `app/exceptions/` | `ErrorCode`, `AppError`, envelope handlers |
| `app/middleware/request_context.py` | raw ASGI: ids, access log, unhandled-error rendering |
| `app/middleware/routing.py` | route template for low-cardinality log fields |
| `app/services/health.py` | `DependencyCheck` protocol; MongoDB, Redis, Qdrant checkers |
| `app/api/routes/health.py` | `/healthz`, `/readyz` |
| `scripts/_common.py` | compose detection, command runner, coloured output, repo paths |
| `scripts/{doctor,setup,up,down,status,logs,cleanup}.py` | one entry point each |
| `deploy/compose.yml` | mongo:8, valkey 8, qdrant, named volumes, health checks |

## Key decisions
- **Scripts in Python, not shell.** One implementation for macOS and Windows. The Makefile and `dev.ps1` only
  forward. This also removes the quoting and template problems seen with shell recipes.
- **No `{{ }}` anywhere outside GitHub Actions.** `ps --format json` replaces Go templates. CI skips the
  `concurrency` expression for the same reason; nothing else in the workflow needs expressions.
- **Health checks with a 1 s timeout each, run concurrently.** A slow dependency can't stall the probe.
- **`/healthz` + `/readyz`** names, so the same probes work on container hosts and on serverless platforms.
- **Coloured logs** use structlog's `ConsoleRenderer`. `colorama` is a Windows-only dependency, so the
  Windows console renders colour too.

## Data model
None in this feature.

## Risks
- `podman compose` and `docker compose` differ in `ps --format json` output: podman prints one JSON array,
  docker prints one object per line. Mitigation: the parser accepts both.
- Ports collide with other local stacks (27017, 6379, 6333). Mitigation: host ports are configurable in `.env`;
  defaults are offset (27018, 6380, 6334/6335), and `doctor` reports busy ports.
