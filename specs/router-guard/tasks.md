# Tasks: router-guard

Each task is one commit. Commit subjects reference the task id.

| ID | Task | Verifies AC |
|---|---|---|
| T1 | `guard_records` in routing.py: reject unearned records routes, drop unmentioned filters; unit tests | 1–4 |
| T2 | Wire the guard into the owner graph's route node with metadata-only logging; graph tests | 1–5 |
| T3 | Real re-run on native Ollama `llama3.2:3b`; dev-notes updated | 6 |
