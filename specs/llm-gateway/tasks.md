# Tasks: llm-gateway

Each task is one commit (a large task may carry several). Commit subjects reference the task id.

| ID | Task | Verifies AC |
|---|---|---|
| T1 | Inventory `capabilities` + `quality`, values for every entry, optional Qwen3 entries | 1 |
| T2 | `Need` + profiler (rules, Laya) | 2 |
| T3 | Gateway fit ordering in selection; `need` in `config/agents.yaml`; agents pass needs | 3, 6 |
| T4 | Reasoning output: Ollama `reasoning=True`, `<think>` stripping, no thinking in logs or traces | 4 |
| T5 | Model-id drift: `make models-check`, doctor line, runtime 404 cool-off | 5 |
| T6 | Docs (`docs/llm-gateway.md`, components, orchestrator README), dev-notes, tests, live run, PR | 1–6 |
