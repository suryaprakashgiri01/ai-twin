# Tasks: deploy-public

| ID | Task | Verifies AC |
|---|---|---|
| T1 | Public-profile document filter trusting `kb_public` | 1 |
| T2 | `make publish`: mirror `kb_public` to the cloud Qdrant, prune, export canary hashes and resume (dry run by default) | 2 |
| T3 | Dockerfile, .dockerignore, Space settings; local build and smoke test | 3 |
| T4 | docs/deploy-public.md, dev-notes | 1–3 |
