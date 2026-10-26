# AI-twin — agent memory & house rules

> **Docs router.** For the daily *update → sync → publish → verify → deploy* loop, read exactly
> one file end-to-end: **`dev-notes/deploy-public.md`** (local-only, kept fresh there; includes
> the logs guide and the pitfalls table mirrored below). `docs/deploy-public.md` is the standing
> one-time setup reference. Don't hunt anywhere else unless asked.

Working notes for AI assistants (Claude Code, OpenCode, …). Keep this file small and factual;
update the pitfalls table whenever a new one costs time. Never add secrets, account identifiers,
keys, or anything personal — this file ships in the repo.

## The one mental model

The repo has two deployment profiles:

| Profile | Depends on | Admin routes | Reads keys from |
|---|---|---|---|
| `vault` (local dev) | local compose stack (Mongo 27018, Valkey 6380, Qdrant 6334) | exist | local Mongo `api_keys` |
| `public` (Cloud Run) | Atlas Mongo, Upstash Redis, Qdrant Cloud, GCS bucket `/publish` | **must not** exist | Atlas `api_keys` |

- Cloud values live in `.env` under different names (`ATLAS_MONGODB_URI`, `UPSTASH_REDIS_URL`,
  `PUBLISH_*`) — **never** merge them into the vault's `MONGODB_URI`/`REDIS_URL`/`QDRANT_URL`.
- GCP Secret Manager is the source for deployed values; `.env.prod` (gitignored) mirrors them
  locally **mapped onto the app's own names**, for testing the deployed world from local.
- API keys live **inside Mongo** (`api_keys` collection, hash-only). Local keys never work
  against the deployment and vice-versa; the plaintext is printed exactly once at creation.

## Frequent commands (Windows PowerShell; `make` wraps python, no bash needed)

```powershell
$env:PYTHONUTF8 = "1"        # first thing in a new shell: scripts print non-ASCII glyphs

make up                      # start local stack, wait for health (make status to check)
make status                  # container state + health
make down                    # stop, keep data
make doctor                  # tools, compose runtime, .env, ports

make keys ARGS="create NAME --scope admin"   # | list | revoke NAME  (prints key once)
make kb-sync ARGS="--apply"  # rebuild/refresh KB from vault/kb/manifest.yaml (dry run first)
make publish                 # dry run: mirror counts
make publish ARGS="--apply"  # mirror kb_public to Qdrant Cloud + write vault/publish/*
gcloud storage cp vault\publish\* gs://<PUBLISH-BUCKET>/

make eval ARGS="--limit 5"   # live public-assistant eval (red team + golden set)
make deploy-env              # pull GCP secrets into .env.prod (values never printed)
make deploy-check ARGS="--url <service-url> [--public-key <key>] [--timeout 5]"
make check                   # quality gate: ruff, ruff format, mypy --strict, pytest+coverage
```

Testing the deployed world from local: `AI_TWIN_ENV_FILE=.env.prod make status` — any target can
run against Atlas/Upstash/Qdrant Cloud this way. Only `pull`/`check` subcommands exist under
`deploy_env`; the make targets pass the right one (`deploy-env` → pull, `deploy-check` → check).

## Update → publish → deploy loop (the repeat cycle)

1. Edit content in the vault (admin API / console / `make chat`); `make kb-sync --apply`.
2. `make publish ARGS="--apply"` then the bucket copy above (canary-hashes + resume).
3. Verify: `make deploy-check ARGS="--url <service-url>"`.
4. Redeploy only if code changed; content steps need no Redeploy.

## Deploy command (single line!)

PowerShell's backtick line continuation can mangle gcloud dict args — keep
`gcloud run deploy` on **one line**. Template and prerequisites: `dev-notes/deploy-public.md`
(gitignored, machine-local; `docs/deploy-public.md` holds the standing reference). Flags give
free-tier behavior: `--min-instances 0 --cpu-throttling --max-instances 1`.

## Secret hygiene (hard rules)

- **Git identity:** every commit must be authored by `suryaprakashgiri01` (noreply or the
  gmail address this account uses — see local git config); never any other name/email or gh
  account. Verify `git config user.name` before the first commit in a session; if wrong,
  fix repo config and `--global` first.

- Never print, log, or echo a secret/key value — not into chat, not into files. Verify by
  version state / length only (`gcloud secrets versions list <name>`).
- Windows: `.env` is CRLF. Always `.Trim(' "').TrimEnd("`r")` when piping a value into a file;
  a stray `\r` breaks httpx headers at runtime (`Illegal header value`).
- Write temp secret files with `-NoNewline`, use them, delete them.
- Secret Manager free tier is a handful of active versions/month: rotate = add + verify +
  **destroy** the old version; config-only changes never bump versions.

## Before you deploy — checklist

- [ ] `make check` green (ruff, ruff format, mypy --strict, pytest)
- [ ] `.env.example` still documents every new setting; `.env*` files stay gitignored
- [ ] No new startup-required env var missing from the deploy command
- [ ] `make deploy-env` refreshed; new secrets created under `ai-twin-*` **before** deploy
- [ ] Pre-commit hooks gpg-clean: no leaks (`detect private key` etc.)
- [ ] Published data current: `make publish` dry run then apply, bucket copy
- [ ] Deploy → `make deploy-check --url …` all green → eyeball the live widget + console

## Coding style (enforced by the gate; matching it avoids churn)

- Python 3.12, `uv`-managed; mypy **--strict** and ruff must be clean — annotate everything,
  delete dead/unreachable code instead of silencing it (`type: ignore` only when unavoidable).
- Helper scripts live in `scripts/` with a docstring y-command summary, plain-ASCII output
  (the ✓/✗ glyphs crash cp1252 shells), and make targets pass `ARGS` through.
- Tests: pytest, async-friendly; route-shape tests (`test_routes_limits`) assert the public
  profile exposes only public+health routes — updated them when routes change.
- Commits: conventional-ish imperative subject; pre-commit runs gitleaks — never commit
  `.env*`, vault/, dev-notes/ (gitignored by design).

## Service logs — console UI vs commands

- UI: console.cloud.google.com → Logs Explorer → resource `Cloud Run revision`, service
  `ai-twin-public` (app JSON logs on stdout: `event`, `level`, `error_type`, `request_id`).
- CLI: `gcloud logging read 'resource.type="cloud_run_revision" AND
  resource.labels.service_name="ai-twin-public"' --limit=40 --order=desc
  --format="value(textPayload, jsonPayload.event, jsonPayload.level)"`
- Failures only: `... AND jsonPayload.event:"failed" --freshness=1h`.
- Timestamp filters get their quotes mangled by PowerShell→gcloud.ps1; prefer `--freshness`.

## Pitfalls actually hit (don't rediscover)

| Symptom | Cause | Fix |
|---|---|---|
| 402 `RESOURCE_EXHAUSTED` from Gemini, "prepayment credits depleted" | key's GCP project has billing with empty credit pool — not free-tier 429 | issue key in a non-billed project |
| Groq 404s on `llama-3.1-8b-instant` | retired 2026-08-16; use `gpt-oss-20b` | inventory `config/models.yaml` |
| Gemini 503 UNAVAILABLE on ordinary prompts | free-tier overload; docs expect fallback noise | Groq is primary; Gemini backup |
| `QdrantCheck` 403 on Cloud | `/readyz` needs the `api-key` header | check sends it (self-host ignores) |
| chat 500: `Index required but not found for "canary"` | filtered payload field needs an index; `PAYLOAD_INDEXES` missed it | `canary: BOOL` added; `ensure_collections` self-heals |
| chat 500: missing `sparse` vector config on an older collection | `_ensure_collection` only checked dense before | now adds the config when missing |
| 404 on `/healthz` with Google's error page, but `/readyz` 200 | Cloud Run ingest claims `/healthz` | liveness served at `/livez`; use that path on Cloud Run |
| container exit(3) at startup, `BLOB_ENCRYPTION_KEY is empty` | required secret missing from `--set-secrets` | deploy list is the source of truth; deploy-check catches drift |
| Dockerfile heredoc → `unknown instruction: IMPORT` | classic Cloud Build parser | code lives in `docker_warmup.py`, `COPY`+`RUN` |
| `make deploy-env` fails `[WinError 2]` | gcloud is a `.cmd` shim on Windows | resolved via `shutil.which` in the script |

## Free tier guardrails (per service)

Secret Manager: version-count discipline above. Cloud Run: 0 min-instances + CPU throttling +
max 1 instance. Upstash/Qdrant/Atlas M0/Groq/Gemini: quota-bounded — watch the dashboards, not
the bill. One-time safety net: a Billing budget with alerts in the console.

## Misc

- Redis URL is `rediss://default:<pass>@<host>.upstash.io:6379` (TLS); the REST URL is unused.
- `--data-file=-` piping works in Git Bash; on Windows prefer the temp-file pattern above.
- `python` on PATH is global; use `.venv\Scripts\python.exe` or `uv run` for repo work.
