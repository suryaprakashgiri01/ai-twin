# Spec: dev-tooling

Status: draft · Owner: Surya Prakash Giri · Ticket: none (personal repo; `tasks.md` is the task list)

## Problem
1. On Windows, `make up` / `make down` and `./scripts/dev.ps1 up|down` stop with
   `FileNotFoundError: [WinError 2]` from `_winapi.CreateProcess`. The scripts launch the compose binary by its
   bare name. Windows `CreateProcess` only appends `.exe`; it does not find `.cmd` / `.bat` shims, and an
   `AI_TWIN_COMPOSE` override naming a binary that is not installed fails the same way, with a traceback instead
   of a hint.
2. On Windows, `make run` fails with "'RELOAD' is not recognized…": the recipe uses POSIX `VAR=value cmd`
   syntax, which `cmd.exe` (GNU make's shell on Windows) does not understand.
3. The fake-persona data used for manual end-to-end checks (notes, a confidential offer, records) is only
   described in the git-ignored `dev-notes/`, so a fresh clone has no way to create it.

## Goals
1. Every subprocess the dev scripts start is resolved with `shutil.which` first (which honours `PATHEXT`), so
   `.exe`, `.cmd` and `.bat` all run. A compose command whose binary is not on `PATH` (including an
   `AI_TWIN_COMPOSE` override) fails with the same install hint as "no compose found", never a traceback.
2. `python -m app.main --reload` turns on reload. `make run` and `./scripts/dev.ps1 run` pass the flag instead
   of setting an environment variable. `RELOAD=true` in `.env` keeps working.
3. `python -m scripts.seed` (`make seed`, `./scripts/dev.ps1 seed`) creates the fake persona (Kavya Testpal)
   through the running API: a private project note, a confidential Globex Labs offer note and a handful of
   application and interview records; the profile only with `--with-profile`, since that replaces the owner's. It issues a temporary admin key directly in MongoDB and
   revokes it when done. It is a dry run unless given `--apply`.
4. `python -m scripts.seed --remove --apply` deletes exactly what the seed created, read from a manifest in
   `vault/seed.json`; real documents and records are never touched. Records are also matched by a fixed marker
   in their `notes`, so they can be removed without the manifest.

## Non-goals
- Changing integration tests: they already create and drop their own `ai_twin_it` database and do not depend
  on seeded data, and `make check` deselects them.
- Seeding the profile on `--remove`: the profile is a single document, so removal leaves it for the owner.
- A Windows CI runner.

## Acceptance criteria
1. With `shutil.which` returning a `.CMD` path for `docker-compose`, the compose argv starts with that full path.
2. `AI_TWIN_COMPOSE=podman compose` without podman installed exits 1 with the install hint, no traceback.
3. `python -m app.main --reload` passes `reload=True` to uvicorn; the Makefile has no `VAR=value` recipe prefix.
4. `scripts.seed` without `--apply` makes no HTTP calls and prints the plan; with `--apply` it submits each note
   once, waits for the jobs, creates the records and writes the manifest; `--remove --apply` deletes the
   manifest's documents and the marked records and deletes the manifest.
5. Unit tests cover 1–4 with fakes; one real run on macOS/podman is recorded in dev-notes.
