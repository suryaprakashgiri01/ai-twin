# Plan: observability-alerts

## Architecture
```
request ─▶ OTel server span (trace_id) ─▶ RequestContext logs (trace_id, request_id)
   └▶ agent run ─▶ Tracing.start(channel, trace_id, tags) ─▶ Langfuse root span (masked per policy)
                       └ LangChain CallbackHandler on orchestrator + graph calls ─▶ generations
   └▶ guards / outcomes ─▶ Tracing.score(...) and Metrics.*.inc()
SecurityEventWriter.record ─▶ MongoDB ─▶ AlertWorker (poll) ─▶ policy: immediate | threshold | digest
                                                         └▶ Mailer (lead SMTP) ─▶ ALERT_EMAIL_TO only
GET /metrics ─▶ prometheus_client registry (vault open; public bearer token)
```

## Module layout

| Module | Responsibility |
|---|---|
| `app/core/telemetry.py` | OTel SDK provider, FastAPI instrumentation, optional OTLP exporter, `X-Trace-ID` |
| `app/core/logging.py` | Presidio/spaCy logger quieting; single handler |
| `app/main.py` | `reload_dirs` for `--reload` |
| `app/observability/tracing.py` | `Tracing` facade over Langfuse v3: start/end trace, callbacks, tags, scores, no-op when off |
| `app/observability/masking.py` | Policy per channel; identifier masking for public text; lead field masking |
| `app/observability/metrics.py` | Metric definitions, `/metrics` route, public bearer check |
| `app/security/alerts.py` | Policies, thresholds, cooldowns (Redis), digest assembly, rendering |
| `app/security/worker.py` | Polling worker; marks `alerted_at` / `digested_at`; retries on SMTP failure |
| `app/api/routes/admin_security.py` | Events list, alert status, test alert |
| services (owner_chat, intake, public_chat, ingestion, leads, titling) | Start a trace per run; pass callbacks; record scores |

## Key decisions
- **Masked by default for owner content.** Langfuse Cloud is a third party, so owner text never leaves the
  machine by default. The trace still shows which model ran, how long it took, the tokens and the fallbacks,
  which covers most debugging.
- **Public text traced, but identifiers masked.** It is public-tier by definition, and it is what persona-evals
  will learn from; visitor PII is still removed.
- **One trace id everywhere.** The OTel span's id seeds the Langfuse trace (pattern proven in a previous
  personal project), so `X-Trace-ID` from Postman or the CLI opens the right trace.
- **Alerts from the stored events, not inline.** The visitor path never waits on SMTP, a crash never loses an
  alert, and the same worker builds digests.
- **Reuse the lead mailer.** One SMTP configuration, one recipient guard, and Brevo already works.
- **Metrics without ids.** Route templates and enums only, so cardinality and privacy are bounded.

## Data model changes
`SecurityEvent` gains `digested_at?` and `surface?` (`admin`/`public`), plus an index on `alerted_at`/`digested_at`
for the worker. New event kinds `llm_unavailable` and `ingestion_failed`. Redis keys `alert:cooldown:<policy>`,
`alert:digest:<YYYYMMDD>`.

## Risks
- **Langfuse SDK and OTel provider interplay.** Use a separate provider for Langfuse, as in the earlier project,
  so app spans don't flood it.
- **Hobby quota (50k units/month).** Sampling setting `LANGFUSE_SAMPLE_RATE` (default 1.0); public traffic can be
  sampled down.
- **SMTP failure during an incident.** It is logged at error level and retried; the PR documents that critical
  alerts depend on SMTP health.
