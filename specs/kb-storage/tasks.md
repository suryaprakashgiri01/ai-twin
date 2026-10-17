# Tasks: kb-storage

Each task is one commit (a large task may carry several). Commit subjects reference the task id.

| ID | Task | Verifies AC |
|---|---|---|
| T1 | Mongo client + Beanie init in lifespan, fail-fast auth, settings additions, `setup` appends new keys | 1 |
| T2 | Beanie documents + indexes for every collection; migrations runner | 1, 4 |
| T3 | Redis client + conversation service (cache-aside, background persist, TTLs, visitor hash) | 3, 4 |
| T4 | Qdrant client + `ensure_collections` per profile (dense + sparse, payload indexes) | 1, 2 |
| T5 | Encrypted content-addressed blob store | 5 |
| T6 | API-key auth: key generation/hash, `require_scopes`, `whoami`, `keys` script, profile-gated admin router | 2, 6 |
| T7 | Model override persistence + admin override routes + audit log | 7 |
| T8 | Vault `export` / `import` scripts with manifest, checksums, chunked encryption | 8 |
| T9 | Tests: unit (fakes) + integration (compose stack); dev-notes E2E steps for macOS and Windows | 1–9 |
