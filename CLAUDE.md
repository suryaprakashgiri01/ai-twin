# ai-twin — project rules

Personal knowledge base + portfolio AI twin. Architecture: `docs/hld.md`. Feature specs: `specs/<feature>/`.

## Data rules (non-negotiable)

- **No personal data in git.** Real notes, documents, exports and secrets live in the git-ignored `vault/` and
  `.env`. Tests and seeds use a fake persona only.
- **No employer data anywhere**: no employer code, internal docs, repo or service names, ticket keys, internal
  URLs, colleague names or customer data. That applies to code, docs, commits and PRs.
- **Default deny.** New content is `private` until the owner approves publishing it. The public path only ever
  reads the `public` collection.
- **Tier caps on models.** A model may only see data at or below its `max_tier`. Never route
  `confidential`/`restricted` text to a cloud API.

## Workflow

- Spec-driven: commit `specs/<feature>/{spec,plan,tasks}.md` first, then one commit per task
  (`feat(scope): T3 …`). A large task may carry several commits; a commit never spans two tasks.
- Branches: `feature/<name>` from `develop` → PR to `develop` → merge after CI. `develop → main` only after
  end-to-end verification; the owner merges that PR.
- Run the full gate **once** before raising a PR: `make check` (Windows: `./scripts/dev.ps1 check`). Pre-commit
  covers lint, format, types and secrets on each commit. Targeted tests are fine for a risky question.
- Stage explicit paths. Never `git add -A` / `git add .`.
- Never credit an AI tool in commits or PRs (no `Co-Authored-By`, no "Generated with").
- Personal GitHub account only: `gh auth switch -h github.com -u suryaprakashgiri01` before any push or PR.

## Code conventions

- Python 3.12, uv, FastAPI, pydantic-settings, structlog. mypy strict, ruff (line length 100).
- Errors: raise `AppError(ErrorCode.X, message)`; the envelope is rendered centrally.
- Logs: `structlog.get_logger(__name__)`; never log secrets, raw documents or visitor messages at INFO.
- Keep functions small (cognitive complexity < 15); follow existing patterns before adding new ones.

## Scripts and containers

- All dev logic is Python in `scripts/` (`uv run python -m scripts.<name>`). The `Makefile` and
  `scripts/dev.ps1` are one-line wrappers, so macOS (podman) and Windows (docker) behave the same.
- **No Go-template double braces** in the Makefile, `deploy/` or `scripts/`. Read container state with
  `--format json` and parse it in Python. A test enforces this.
- Destructive scripts are dry runs unless given `--apply`.

## Local notes

`dev-notes/` is git-ignored: setup, E2E testing, runbook, future planning. Keep it in sync with API and script
changes, with every command in macOS and Windows (PowerShell 7) form.

## Token-saving tools

- RTK rewrites shell output through a global hook; treat condensed output as complete and re-run with
  `rtk proxy <cmd>` only when a result is unusable.
- graphify (section below) answers structure questions from a code graph before reading files.

## graphify

This project has a knowledge graph at graphify-out/ with god nodes, community structure, and cross-file relationships.

Rules:
- For codebase questions, first run `graphify query "<question>"` when graphify-out/graph.json exists. Use `graphify path "<A>" "<B>"` for relationships and `graphify explain "<concept>"` for focused concepts. These return a scoped subgraph, usually much smaller than GRAPH_REPORT.md or raw grep output.
- If graphify-out/wiki/index.md exists, use it for broad navigation instead of raw source browsing.
- Read graphify-out/GRAPH_REPORT.md only for broad architecture review or when query/path/explain do not surface enough context.
- After modifying code, run `graphify update .` to keep the graph current (AST-only, no API cost).
