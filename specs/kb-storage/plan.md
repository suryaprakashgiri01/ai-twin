# Plan: kb-storage

## Architecture
Storage adapters sit behind small repositories, so the agents and ingestion code never touch drivers directly.
The app lifespan opens one `AsyncMongoClient`, one Redis client and one Qdrant client, initialises Beanie, runs
idempotent bootstrap steps (indexes, collections), and closes everything on shutdown.

```
lifespan ─▶ Mongo (Beanie init + indexes + migrations) ─▶ Redis ─▶ Qdrant (ensure collections) ─▶ app.state.storage
routes/services ─▶ repositories ─▶ { Beanie documents | Redis | Qdrant | BlobStore }
```

## Module layout

| Module | Responsibility |
|---|---|
| `app/db/client.py` | Mongo client lifecycle, Beanie init, fail-fast auth error |
| `app/db/documents.py` | Beanie `Document` classes + index declarations (the data model table) |
| `app/db/migrations.py` + `migrations/` | Versioned, idempotent data migrations with an applied-marker |
| `app/cache/redis.py` | Redis client lifecycle, key helpers |
| `app/services/conversations.py` | Cache-aside history, `append_turn`, background persist with retry |
| `app/vector/qdrant.py` | Qdrant client, `ensure_collections(profile)` |
| `app/storage/blobs.py` | Fernet, content-addressed blob store |
| `app/auth/{keys,dependencies}.py` | Key generation/hash, `require_scopes` |
| `app/api/routes/admin_models.py`, `admin_meta.py` | Override routes, `whoami` |
| `app/services/audit.py` | Audit log writer |
| `scripts/keys.py`, `scripts/export.py`, `scripts/import_.py` | Key management, vault export/import |
| `scripts/_crypto.py` | scrypt key derivation + streamed Fernet chunks for archives |

## Key decisions
- **Beanie + PyMongo Async.** Motor is deprecated. Beanie gives pydantic models that match the rest of the
  app and are typed for mypy strict.
- **Messages in their own collection**, not embedded arrays. Conversations are unbounded, a document has a 16 MB
  cap, and TTL works per message.
- **Background persistence via an in-process task with retry.** No queue system is needed at this scale. A
  crash loses at most turns still sitting in Redis, which keeps them for their TTL.
- **Archive encryption with a passphrase, not the `.env` key.** The archive must be openable on a new machine
  whose `.env` is regenerated.
- **Archive written in chunks.** Each part is Fernet-encrypted in 8 MiB frames, so a large Qdrant snapshot never
  sits fully in memory.
- **`mongodump` inside the container** (`compose exec -T mongodb mongodump --archive`). Neither machine needs
  MongoDB tools installed.

## Risks
- Qdrant snapshot restore differs across versions; pin the image and record the version in the manifest.
  `import` refuses a mismatch unless forced.
- TTL deletion is approximate (the monitor runs about every 60 s); tests assert `expires_at`, not deletion.
- Windows path handling for blobs and archives: use `pathlib` everywhere; the Windows run is part of E2E.
