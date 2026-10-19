# Plan: dev-tooling

| Module | Change |
|---|---|
| `scripts/_common.py` | `resolve(argv)` → full path via `shutil.which` or `None`; `run`, `compose_exec_binary` and `podman_docker_host` use it; `require_compose` fails with the hint when the prefix does not resolve |
| `app/main.py` | `--reload` flag (argparse) OR'd with `settings.reload` |
| `Makefile`, `scripts/dev.ps1` | `run` passes `--reload`; new `seed` target / command |
| `scripts/seed.py` | Fixtures (fake persona only), `plan`, `apply` and `remove` over an `httpx.Client`; temp key via `issue_key` + `MongoApiKeyStore`, revoked in `finally`; manifest at `vault/seed.json` |

## Seed flow
```
issue temp admin key (MongoDB) ─▶ PUT /admin/v1/profile
                                ─▶ POST /admin/v1/ingest/text × notes ─▶ poll /sources/{id} until terminal
                                ─▶ POST /admin/v1/records × records (notes carry the seed marker)
                                ─▶ write vault/seed.json ─▶ revoke key
remove: DELETE /documents/{id} (manifest) + DELETE /records/{id} (manifest ∪ marker) ─▶ delete manifest
```

## Tests
`tests/test_scripts.py` (resolve, override hint, Makefile has no env-prefix recipe), `tests/test_seed.py`
(httpx `MockTransport`, no network), `tests/test_main.py` (reload flag with uvicorn patched).
