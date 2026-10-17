# Spec: bootstrap

Status: approved · Owner: Surya Prakash Giri · Ticket: none (personal repo; `tasks.md` is the task list)

## Problem
ai-twin needs a foundation that every later feature builds on: a runnable FastAPI service with structured,
coloured logs and one error envelope, and a local stack (MongoDB, Valkey, Qdrant). That stack has to start
the same way on macOS with podman and on Windows with docker. The repo also needs quality gates, CI and
AI-assistant tooling. None of it may carry personal data.

## Goals
1. `uv run python -m app.main` serves `GET /healthz` (liveness) and `GET /readyz` (MongoDB, Redis, Qdrant).
2. Logs are structlog: coloured console on a TTY, JSON when piped or when `LOG_FORMAT=json`. Every line carries
   `request_id` and `session_id`, and secret-looking keys are redacted.
3. Every error renders as `{"error": {code, message, request_id, details?}}`.
4. One command set works on both machines: `scripts/*.py` holds the logic, `Makefile` (macOS) and
   `scripts/dev.ps1` (Windows) are one-line wrappers.
5. Compose runtime is detected in the order `docker compose`, `docker-compose`, `podman compose`, and can be
   overridden with `AI_TWIN_COMPOSE`.
6. No Go-template `{{ }}` syntax appears in the Makefile, compose files or scripts. Container state is read as
   JSON and parsed in Python.
7. Destructive scripts (`cleanup`) are dry runs unless given `--apply`.
8. Quality gate `check` = ruff lint + format check, mypy strict, pytest with ≥80% coverage. Pre-commit runs
   ruff, mypy, gitleaks and the standard hygiene hooks. CI runs the same gate on pushes to `main`/`develop`
   and on every PR.
9. Repo carries `CLAUDE.md`, a committed `.claude/settings.json` with graphify hooks, and a git-ignored
   `dev-notes/` and `vault/`.

## Non-goals
LLM calls, storage models, ingestion, agents, auth, tracing exporters, deployment. Each has its own feature.

## API

| Method | Path | Purpose |
|---|---|---|
| GET | `/healthz` | Liveness: process up, returns version |
| GET | `/readyz` | Readiness: each dependency `ok` or `unavailable`; 503 when any required one is down |

`/readyz` 200 body: `{"status": "ready", "checks": {"mongodb": "ok", "redis": "ok", "qdrant": "ok"}}`.

### Error envelope

| Code | Status |
|---|---|
| `VALIDATION_ERROR` | 422 |
| `AUTH_REQUIRED` | 401 |
| `AUTH_INVALID` | 401 |
| `FORBIDDEN` | 403 |
| `NOT_FOUND` | 404 |
| `RATE_LIMITED` | 429 |
| `DEPENDENCY_UNAVAILABLE` | 503 |
| `INTERNAL_ERROR` | 500 |

## Scripts

| Script | Behaviour |
|---|---|
| `doctor` | Reports uv, Python, compose runtime, free ports, `.env` presence and missing keys (names only). Exits 1 on a blocking problem. |
| `setup` | Creates `.env` from `.env.example` if absent, `uv sync`, `pre-commit install`, starts the stack, waits for health. |
| `up` / `down` / `status` / `logs` | Compose lifecycle; `status` prints a table from `ps --format json`. |
| `cleanup` | Lists containers, volumes and local caches it would remove; removes them only with `--apply`. |

## Observability requirements
- One access log line per request: method, route template, status, duration.
- Probe paths (`/healthz`, `/readyz`) log at DEBUG unless they fail.

## Acceptance criteria
1. `GET /healthz` returns 200 with `status` and `version`.
2. `GET /readyz` returns 200 when all checks pass and 503 with per-check status when any fails.
3. An unknown path returns the envelope with `NOT_FOUND` and the request id; invalid input returns
   `VALIDATION_ERROR` without echoing the input.
4. Responses carry `X-Request-ID` and `X-Session-ID`; a valid caller id is echoed, an invalid one replaced.
5. Console logs are coloured on a TTY, JSON otherwise; `password`/`api_key`/`authorization`/`secret`/`token`
   values are redacted.
6. Compose detection picks the first working runtime and honours `AI_TWIN_COMPOSE`.
7. `cleanup` without `--apply` changes nothing.
8. `make check` passes locally and in CI.
9. No file in the Makefile, `deploy/` or `scripts/` contains `{{`.

## Open questions
None.
