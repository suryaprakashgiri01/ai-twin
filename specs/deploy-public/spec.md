# Spec: deploy-public (F9)

## Why
The portfolio's assistant has to run somewhere public, with published knowledge only, while the vault stays on
the owner's machine.

## Decisions
- **Host.** Google Cloud Run in `asia-south1` (Mumbai), next to the Upstash Redis and the Atlas cluster. Hugging
  Face Spaces were the first choice but block outbound ports other than 80, 443 and 8080, so they cannot reach
  MongoDB Atlas (27017) or Redis (6379). Any container host without that limit works with the same
  image (Fly, Render).
  - Vercel was the first idea, but its Python functions cannot hold this app: the embedding, reranking and
    redaction models exceed its function size limits.
  - The portfolio (on Vercel) calls the API from its server route with the public key, as before.
- **Data in the cloud** (free tiers):
  - MongoDB Atlas: keys, visitor conversations, leads, security events.
  - Upstash Redis: rate limits, cache, conversation history.
  - Qdrant Cloud: `kb_public` only.

  Nothing else leaves the vault.
- **`kb_public` is the source of truth on the public profile.** The vault's `kb_documents` records stay home.
  Publishing writes only published, public-tier chunks to `kb_public`, and unpublishing removes them. So the
  public retriever trusts what is in the collection instead of checking MongoDB.

## What changes
1. **Public retriever filter.** On `DEPLOYMENT_PROFILE=public`, a filter that treats every `kb_public` hit as
   live. The vault keeps the MongoDB check.
2. **`make publish`** (Windows: `./scripts/dev.ps1 publish`) mirrors the vault's `kb_public` collection, with
   vectors and payloads, to the cloud Qdrant at `PUBLISH_QDRANT_URL` / `PUBLISH_QDRANT_API_KEY`.
   - It deletes cloud points that are no longer published.
   - It writes the canary hash set (hashes only) to `vault/publish/canary-hashes.json`, and the profile's
     resume file to `vault/publish/resume.pdf`, for the host's secret files.
   - It is a dry run unless given `--apply`: counts only, no values printed.
3. **Image.**
   - `Dockerfile` (python 3.12-slim, uv, non-root user, models fetched at build time).
   - It defaults to `DEPLOYMENT_PROFILE=public` and port 8080 (Cloud Run sets `PORT`).
   - A `.dockerignore` keeps `vault/`, `.env`, `dev-notes/` and every data file out of the image.
4. **Docs.** `docs/deploy-public.md`: the accounts to create, the Space's variables and secrets, publish, smoke
   test, and rollback. Every command in macOS and PowerShell form in the dev-notes.

## Not in scope
Creating the accounts or the Space, and the first deploy. Those are the owner's steps (the docs list them);
nothing is published without the owner running `make publish --apply`.

## Acceptance
1. On the public profile the retriever returns `kb_public` hits without a `kb_documents` collection (test).
2. `make publish` without `--apply` changes nothing and prints counts. With `--apply` against a test Qdrant, it
   mirrors and prunes (test with an in-memory client).
3. The image builds, and contains no `vault/`, `.env` or `dev-notes/`. Started against the local stack, it
   answers `/healthz`, and the admin routes return 404.
