<!-- ai-twin PR template — base branch is `develop` (main gets release PRs only) -->

## What

<!-- 3–4 bullets max; bold the make targets and key paths touched. Link the spec: specs/<feature>/spec.md -->

## Tasks

<!-- Task ids from specs/<feature>/tasks.md covered by this PR (delete if no spec) -->

## Verification

<!-- How it was verified: `make check` output summary, live smoke, deployed revision # -->

## Checklist

- [ ] `make check` green locally (ruff, ruff format, mypy --strict, pytest, ≥80% coverage)
- [ ] `.env.example` updated for any new env var; no `.env*`, `vault/`, or `dev-notes/` content committed
- [ ] No personal data, employer data, employer names, or secrets in the diff; no gratuitous Secret Manager version churn (free tier)
- [ ] Docs updated where relevant (`docs/deploy-public.md` is the standing ref; runbook is local-only)

## Review order

<!-- Files in read order, riskiest first, one line on why it's risky -->
1.
