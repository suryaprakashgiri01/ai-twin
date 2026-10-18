# Plan: opportunities

## Architecture
```
/v1/chat ─▶ public graph ─ scope_router: opportunity ─▶ capture loop
   extract (tier public, structured) ─▶ validate (code) ─┬ missing ─▶ ask one question ─▶ END turn
                                                          └ complete ─▶ confirm (interrupt; MongoCheckpointSaver ns=public)
   yes + Turnstile ─▶ caps / dedupe ─▶ triage (Jev FF | classifier) + fit note ─▶ Lead(status=new|suppressed)
                                                                                     └▶ outbox worker ─▶ SMTP (owner only)
/v1/opportunities (leads:write) ─▶ same validate ─▶ caps ─▶ triage ─▶ Lead ─▶ outbox
/admin/v1/leads (list, patch) · intake: show_leads
```

## Module layout

| Module | Responsibility |
|---|---|
| `app/leads/models.py` | `LeadDraft`, `LeadIn`, validation, missing-field order, statuses |
| `app/leads/capture.py` | Capture nodes for the public graph: extract, validate, ask, confirm, exit |
| `app/leads/triage.py` | Choice classification + deterministic fit note |
| `app/leads/service.py` | Caps, dedupe, store, enqueue; shared by chat and form endpoint |
| `app/leads/outbox.py` | Background sender, retries, digest of suppressed leads |
| `app/leads/email.py` | `aiosmtplib` client, subject/body rendering (escaped), Reply-To |
| `app/leads/turnstile.py` | Siteverify client (httpx), off when no secret |
| `app/agents/graphs/public.py` | Wire the capture loop and checkpointer into the public graph |
| `app/api/routes/public.py`, `admin_leads.py` | Form endpoint; admin list/patch |
| `app/agents/intake/actions.py` | `show_leads` |

## Key decisions
- **Code validates, the model extracts.** As in intake: an invalid or missing field costs one more question,
  never a bad lead.
- **Outbox, not inline SMTP.** The visitor isn't kept waiting on Gmail, and failures retry and surface instead of
  being lost.
- **No outbound mail to visitor addresses.** It removes the whole spam-relay class of abuse; the owner replies
  from their inbox.
- **Reuse the intake checkpointer.** One implementation, namespaced, with a shorter TTL for public drafts.
- **Fit note is deterministic.** Explainable in the email ("matches: backend, remote"), and it doesn't depend on
  a model mood.

## Data model changes
`Lead` gains `visitor_hash?`, `source` (`assistant | form`), `details{location?, employment_type?, link?}`,
`status`, `triage_confidence?`, `fit_note?`, `attempts`, `last_error_code?`, `updated_at`, `expires_at` (TTL),
and an index on `email` + `company` + `created_at` for dedupe. Migration 0005 sets defaults on existing leads.
Redis keys: `lead:v:<hash>:day`, `lead:global:<day>`.

## Risks
- **Gmail app passwords need 2FA, and Google may rate-limit.** Volume is tiny; failures surface via `failed`
  plus an event.
- **A free-model summary could include injected text.** The summary is generated over public text, passes the
  output guard, and is HTML-escaped; the visitor's raw message is shown as quoted text, never as markup.
- **Turnstile off locally.** It is verified only when the secret is set; the PR states the mode used.
