# Tasks: bootstrap

Each task is one commit (a large task may carry several). Commit subjects reference the task id.

| ID | Task | Verifies AC |
|---|---|---|
| T1 | Project skeleton: uv workspace, settings, logging, context, error envelope, request-context middleware, health routes, app factory, tests | 1, 2, 3, 4, 5 |
| T2 | Local stack: `deploy/compose.yml` (mongo, valkey, qdrant) + `.env.example` | 2 |
| T3 | Cross-platform scripts (doctor, setup, up, down, status, logs, cleanup), Makefile, `scripts/dev.ps1`, tests | 6, 7, 9 |
| T4 | Quality gates: pre-commit (ruff, mypy, gitleaks, hygiene), CI workflow, PR template | 8 |
| T5 | AI tooling: `CLAUDE.md`, `.claude/settings.json` (graphify hooks), graphify skill, ignore rules | — |
| T6 | Docs: threat model, README quick start | — |
