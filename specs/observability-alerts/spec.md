# Spec: observability-alerts

Status: draft, awaiting approval · Owner: Surya Prakash Giri · Ticket: none (personal repo; `tasks.md` is the task list)

## Problem
The service logs well but can't be watched.
- **No tracing.** There's no trace view of what each agent run did: which model, prompt version, tokens,
  latency, guard verdicts.
- **No metrics.** Nothing shows a provider failing or the free quotas burning.
- **No alerts.** Security events are stored in MongoDB, but nobody is told. A canary leak or an injection burst
  would go unnoticed until someone opens Compass.

All of this has to stay free, and it must not become a new leak: owner and visitor text can't simply be shipped
to a tracing SaaS.

Owner testing also left some noise to clean up:
- `--reload` watches `vault/` and `graphify-out/`.
- Presidio logs every unmapped spaCy entity type, twice.

## Goals
1. **Log hygiene and correlation:**
   - Reload watches only `app/`, `packages/` and `config/`.
   - Presidio logs warnings once, without the "entity not mapped" spam.
   - An OpenTelemetry SDK `TracerProvider` creates a server span per request, so every log line carries
     `trace_id` and `span_id`, and responses carry `X-Trace-ID`.
   - An optional OTLP exporter (`OTEL_EXPORTER_OTLP_ENDPOINT`) is off by default.
2. **Langfuse tracing (Cloud Hobby, free; or self-hosted)** when `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` /
   `LANGFUSE_BASE_URL` are set. Without them it's a no-op.
   - One Langfuse trace per owner chat, intake turn, public chat, ingestion job, lead submission and title
     refresh. The trace id is the request's OTel trace id, so logs, `X-Trace-ID` and Langfuse match.
   - LLM calls (orchestrator and LangGraph nodes) are recorded through the LangChain callback handler with
     model, role, tier, prompt version, tokens, latency and fallback attempts.
   - Tags and metadata: channel (`owner`, `intake`, `public`, `ingestion`, `leads`), route/intent, max tier,
     `cache_hit`, guard verdicts (rule names), `env`, service version.
   - Scores: grounding verdict, output-guard pass/block, intake outcome (`written`, `discarded`, `asked`), lead
     triage label + confidence.
   - **Masking policy (default-safe):**
     - **Owner channels** (owner chat, intake, ingestion, title refresh): inputs and outputs are replaced by
       `[owner content masked: N chars]`. Structure, timings, models and tokens remain.
       `LANGFUSE_OWNER_CONTENT=true` opts in, for a self-hosted Langfuse only; the app refuses that setting with
       a Langfuse Cloud URL.
     - **Public channel:** visitor messages and answers are public-tier and are traced, after the identifier
       recognisers mask emails, phones and Indian IDs. `LANGFUSE_PUBLIC_CONTENT=false` masks them too.
     - **Lead fields** (name, email, message) are always masked.
   - Flushed on shutdown. A Langfuse outage never fails a request: errors are logged once per minute.
3. **Prometheus metrics** at `GET /metrics`.
   - Vault profile: no auth (localhost). Public profile: `Authorization: Bearer <METRICS_TOKEN>`, and 404 when
     no token is set.
   - Low-cardinality series:
     - HTTP requests by route template, method, status + latency histogram
     - LLM attempts by model, role, tier, outcome + latency and tokens
     - guard decisions by layer and outcome; scope intents
     - ingestion jobs by final status; leads by status; emails by kind and outcome
     - security events by kind and severity; alert emails by policy and outcome; answer-cache hits
4. **Security alert emails**, reusing the lead mailer and SMTP settings.
   - Recipient: `ALERT_EMAIL_TO` (defaults to `LEAD_EMAIL_TO`). Same rule as leads: the owner's address only,
     never a visitor's.
   - **Policy (config table; immediate means within one worker pass):**

     | Event kind | Policy |
     |---|---|
     | `canary_hit` | immediate, always |
     | `output_blocked` | immediate when ≥ 3 in 10 min, else digest |
     | `input_blocked` | immediate when ≥ 5 from one visitor in 10 min, else digest |
     | `auth_failed` (admin and public) | immediate when ≥ 5 in 10 min, else digest |
     | `lead_delivery_failed` | immediate |
     | `meta_probe`, `rate_limited`, `lead_refused`, `grounding_fail_open` | digest |
     | ops: `llm_unavailable` (every model in a chain failed), `ingestion_failed` | digest; immediate when ≥ 5 in 10 min |

   - Per-policy cooldown (default 30 min): a storm sends one email and a count, not hundreds.
   - **Daily digest** at `ALERT_DIGEST_HOUR` (default 09:00) in `OWNER_TIMEZONE`: counts by kind and severity,
     top rules, first and last time. Skipped when there's nothing to report.
   - Emails hold kinds, severities, counts, rule names, short visitor-hash prefixes and times. **Never message
     text, document text, names or emails.** Subject: `ai-twin alert · <severity> · <kind>` or
     `ai-twin daily security digest · <date>`.
   - An alert worker (pattern of the lead outbox) marks `alerted_at` / `digested_at`. If SMTP itself fails it
     logs at error level and retries on the next pass, without losing events.
   - Admin auth failures are now recorded as `auth_failed` with `surface: admin`. New `ops` event kinds:
     `llm_unavailable`, `ingestion_failed`.
5. **Admin API (vault):**
   - `GET /admin/v1/security/events` (filters: kind, severity, since; newest first; no text)
   - `GET /admin/v1/alerts/status`: policies, cooldowns, last sent, SMTP configured
   - `POST /admin/v1/alerts/test`: sends one test alert, audited

## Non-goals
Grafana dashboards and Prometheus alert rules (v2), self-hosting Langfuse (documented only), paging or Slack,
evaluations and datasets (persona-evals), log shipping to a SaaS.

## Acceptance criteria
1. With reload on, writing to `vault/` or `graphify-out/` doesn't restart the server. Ingesting a document logs
   no "Entity … is not mapped" line, and no Presidio line twice.
2. Every request log line has `trace_id`; the response's `X-Trace-ID` matches it.
3. Without Langfuse keys nothing is sent and nothing fails. With keys (or a fake exporter in tests), an owner chat
   produces one trace with the request's trace id, the LLM generations with model, tokens and latency, and
   **masked** input/output.
4. A public chat trace contains the question and answer with emails, phones and IDs masked; lead fields are
   masked. `LANGFUSE_OWNER_CONTENT=true` with a Cloud URL is refused at startup.
5. Langfuse unreachable → requests succeed and at most one warning per minute is logged.
6. `/metrics` in the vault returns Prometheus text with the listed series. In the public profile it is 404
   without a token and 401 with a wrong one. Labels never include ids, names or text.
7. A `canary_hit` sends one immediate email. Three `output_blocked` in 10 min send one email, a fourth inside
   the cooldown sends none, and the count appears in the next email. A single `meta_probe` sends no immediate
   email but appears in the next digest.
8. The digest is sent once per day after `ALERT_DIGEST_HOUR`, is skipped when empty, and never includes text,
   names or emails (asserted on the rendered message).
9. Alert email recipients are only `ALERT_EMAIL_TO`. With SMTP failing, events stay un-alerted and are retried.
10. Admin auth failures create `auth_failed` events with `surface: admin`. Every model in a chain failing
    creates `llm_unavailable`.
11. Admin routes list events without text, show alert status, and send an audited test alert.
12. `make check` passes. A live run shows a trace in Langfuse when the owner has set keys, otherwise the fake
    exporter; one real alert email over the configured SMTP (Brevo). The PR states which.

## Open questions
None blocking. The owner creates a free Langfuse Cloud project (US or EU) and puts its keys in `.env` for the
live check.
