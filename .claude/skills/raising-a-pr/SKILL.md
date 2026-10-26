---
name: raising-a-pr
description: Use whenever opening or updating a pull request in this repo (~ai-twin) — provides the PR template, checklist, and one-command flow prefilled with this repo's values (base branch, gh account, verify commands). Avoids re-deriving details each time; saves tokens.
---

# Raise a PR — ai-twin (prefilled, one flow)

Use this every time a PR is opened in this repo. Do not re-derive anything below; it is
prefilled for ai-twin. Keep bodies short — reviewers read the first 10 lines.

## Hard rules (already established; do not re-ask)

- Identity: commits and all `gh` ops as **suryaprakashgiri01** (see personal memory).
- Base branch: **`develop`** (not main — `main` gets develop→main release PRs only).
- Head branch: single purpose, `feat/…`, `fix/…`, or `deploy/…`.
- **NEVER pass `--body` inline in PowerShell** — backticks are PowerShell escapes and mangle
  markdown. Always `--body-file <temp-md>` (write file → edit → delete).
- One PR = one subject; unrelated cleanup is a separate tiny PR.

## Template (fill the placeholders, keep the sections)

```markdown
## What
<n-4 bullets max; bold the make targets/paths>

## Why / verification
<how it was verified locally: gate output, live smoke, revision #>

## Checklist
- [ ] `make check` green locally (ruff, ruff format, mypy --strict, pytest ≥80%)
- [ ] `.env.example` updated for any new env var; no `.env*`/vault/dev-notes content committed
- [ ] No secrets/PII in diffs or deprecation of free-tier limits (secret versions, budget)
- [ ] Docs touched where needed (`docs/deploy-public.md` standing ref; runbook is local-only)
- [ ] Review order given below (files in read order, riskiest first)

## Review order
1. <file — one line why/risk>
2. <…>
```

## Flow (copy-paste commands; Windows PowerShell)

```powershell
$env:PYTHONUTF8 = "1"
# 0. sanity first (from personal memory)
git config user.name; git config user.email          # must be suryaprakashgiri01 / gmail
# 1. branch + gate
git checkout -b <type>/<subject>
make check                                           # full gate; fix before pushing
git add -A; git commit -m "<type>: <imperative subject>"
git push -u origin HEAD
# 2. body via FILE, then open
Write-Output "" > $env:TEMP\pr-body.md               # fill the template above into this file
gh pr create --base develop --head <branch> --title "<type>: <subject>" --body-file "$env:TEMP\pr-body.md"
Remove-Item "$env:TEMP\pr-body.md"
# 3. wait for checks, then report the URL + initial check status only (never poll repeatedly)
gh pr checks <n>
```

## CI expectations in this repo

- Jobs: `check` (gate re-runnable via `workflow_call`), `image` (docker build), `secrets`
  (gitleaks history scan). All three must pass.
- If `check` fails on Linux but passed locally on Windows: suspect CRLF leftovers, stale
  `# type: ignore` (win32-only typeshed attrs), or cp1252-only glyphs in scripts — plain
  ASCII output in `scripts/`.
- After any fix commit: identity must still be correct before push (`--reset-author` if a
  tool rewrote it).

## Post-merge (release path in this repo)

feature PR → `develop` merged → run `make release-bump ARGS="patch|minor|major"` in a
`release/x.y.z` branch → open the develop→`main` PR citing the feature PRs → merge →
`.github/workflows/deploy.yml` auto-deploys to Cloud Run → verify with
`make deploy-check ARGS="--url <service-url>"`.
