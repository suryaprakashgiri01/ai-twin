# Tasks: observability-alerts

Each task is one commit (a large task may carry several). Commit subjects reference the task id.

| ID | Task | Verifies AC |
|---|---|---|
| T1 | Log hygiene: reload dirs, Presidio noise and duplicate handler; OTel SDK provider + request spans + `X-Trace-ID` (+ optional OTLP) | 1, 2 |
| T2 | Langfuse `Tracing` facade + masking policy; wire owner chat, intake, public chat, ingestion, leads, title refresh | 3, 4, 5 |
| T3 | Scores (grounding, output guard, intake outcome, lead triage) and tags | 3 |
| T4 | Prometheus metrics + `/metrics` (vault open, public bearer) | 6 |
| T5 | New event kinds (admin `auth_failed`, `llm_unavailable`, `ingestion_failed`); alert policies, cooldowns, digest, worker, rendering | 7, 8, 9, 10 |
| T6 | Admin security routes (events, alert status, test alert) | 11 |
| T7 | Tests (fake Langfuse exporter, fake SMTP, metrics text), live run, `.env.example`, dev-notes + Postman | 1–12 |
