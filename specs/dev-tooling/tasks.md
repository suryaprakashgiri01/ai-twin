# Tasks: dev-tooling

Each task is one commit. Commit subjects reference the task id.

| ID | Task | Verifies AC |
|---|---|---|
| T1 | Resolve subprocess executables with `shutil.which`; clear failure for an unresolvable compose | 1, 2 |
| T2 | `app.main --reload`; `make run` and `dev.ps1 run` use it | 3 |
| T3 | `scripts/seed.py` with `--apply` / `--remove`, `make seed`, `dev.ps1 seed`; tests | 4, 5 |
| T4 | Real run on macOS/podman; dev-notes for macOS and Windows | 5 |
| T5 | Stop libraries (magika via markitdown) copying `.env` into `os.environ`; fixes the order-dependent readyz test on clones with a filled `.env` | 6 |
