# Spec: opportunities

Status: draft, awaiting approval · Owner: Surya Prakash Giri · Ticket: none (personal repo; `tasks.md` is the task list)

## Problem
Recruiters and clients who reach the portfolio assistant with an opportunity get a fixed "use the contact form"
reply, so the conversation's context is lost. The assistant should take the details in the conversation,
confirm them with the visitor, store the lead and email the owner. That message should be similar to the
portfolio's "contact me" email but richer: structured fields, a short summary of the conversation, and a triage
note. The flow must not become a spam relay, an injection path, or a leak of the owner's data.

## Goals
1. **Lead capture in the public graph.** The `opportunity` intent enters a capture loop instead of the fixed
   reply.
   - Fields:
     - required: `name`, `email`, `company`, `role` (title or engagement), `message` (what the opportunity is)
     - optional: `location` (or remote), `employment_type` (full-time/contract/freelance), `link` (a job
       description URL, stored as text, never fetched or rendered as a link)
   - Extraction: the classifier/generator role at tier `public` (visitor text is public-tier) fills a draft.
     **Code validates it** (email syntax, lengths, allowed `employment_type`), not the model, and asks for the
     missing required fields one short question at a time.
   - Turn cap `LEAD_MAX_TURNS` (default 6), then it falls back to the contact form reply.
   - A visitor can leave the flow at any time ("never mind"), or ask about the owner in between and come back.
2. **Confirmation:** the complete draft is shown as a short summary and the graph pauses (LangGraph `interrupt`,
   reusing `MongoCheckpointSaver` with a `public` namespace).
   - Only an explicit yes submits. "No" or an edit goes back to the draft.
   - Pending drafts are pruned after 24 h.
   - The visitor never sees an internal id. They get "Thanks — I've passed this to Surya" plus the expected reply
     time from the profile (`reply_time`, optional).
3. **Abuse controls:**
   - Cloudflare Turnstile token (free) verified server-side on submit when `TURNSTILE_SECRET_KEY` is set. The
     portfolio widget passes it through.
   - Caps: 2 leads per visitor per day and `LEAD_DAILY_CAP` (default 30) globally; over a cap → polite refusal
     plus a security event.
   - Duplicate (same email + company within 24 h) → merged into the existing lead, no second email.
   - All existing input/output guards still run on every turn.
   - **No email is ever sent to a visitor-supplied address**: no auto-acknowledgement, so the service can't be
     used as a relay.
4. **Triage:** a `choice` classification at tier public:
   - labels: `recruiter`, `hiring_manager`, `client`, `collaboration`, `spam`, `other`
   - Jev when `FF_JEV_ENABLED`, otherwise the LLM classifier
   - plus a deterministic **fit note**: overlap between the opportunity text and the owner's public
     `preferred_roles`, `locations` and published skill categories
   - `spam` → stored with status `suppressed` and no email; it goes into a daily digest instead.
5. **Email to the owner** via SMTP (Gmail app password, free; or any SMTP).
   - Settings: `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `LEAD_EMAIL_TO`, `LEAD_EMAIL_FROM`.
   - Subject: `Portfolio AI · New opportunity: <role> at <company>` (CR/LF stripped, length-capped).
   - Plain-text and HTML parts; every value HTML-escaped.
   - Body sections: fields table, the visitor's own message, a 3–5 line conversation summary (generator at tier
     public over the public conversation only), triage label + confidence, fit note, received time in
     `OWNER_TIMEZONE`, a lead id for the owner.
   - `Reply-To` is the visitor's address, so replying works; `From` is always the owner's address.
   - **Outbox:** the lead is stored first, then sent by a background worker with 3 attempts and backoff. Final
     failure → status `failed` + a security/ops event. Delivery never blocks the visitor's reply.
6. **Direct form endpoint:** `POST /v1/opportunities` (scope `leads:write`) with the same fields, Turnstile,
   caps, triage and email. The portfolio's contact form can switch to it (portfolio feature) and stop calling
   nodemailer directly.
7. **Owner side (vault):**
   - `GET /admin/v1/leads` (filter by status/triage, newest first) and `PATCH /admin/v1/leads/{id}` (`status`:
     `new | reviewed | replied | archived`, audited).
   - The intake assistant gains a read-only `show_leads` action.
   - Lead retention `LEAD_RETENTION_DAYS` (default 365, TTL index).
8. **Privacy and logs:**
   - The visitor's email is stored as given, but logs carry only a salted hash.
   - Names, messages and summaries never appear in logs at INFO+.
   - The widget's privacy note says details are shared with the owner.

## Non-goals
Auto-replies to visitors, calendar booking, CRM sync, attachments (CV uploads), the portfolio widget itself
(portfolio feature), alerting on general security events (observability-alerts).

## Acceptance criteria
1. "I'm hiring for a backend role at Acme" → the assistant asks only for the missing required fields. A complete
   draft is summarised and the flow pauses. Nothing is stored or emailed before an explicit yes.
2. An invalid email is re-asked. A `link` is stored as plain text and never fetched. Exceeding `LEAD_MAX_TURNS`
   ends with the contact-form fallback.
3. After "yes": exactly one lead (status `new`), one email to `LEAD_EMAIL_TO` with `Reply-To` = visitor,
   escaped HTML, a subject without CR/LF, and the summary, triage and fit note present.
4. No outbound mail goes to the visitor address in any path (asserted on the fake SMTP).
5. A duplicate email + company within 24 h → no new lead and no second email. The per-visitor cap (3rd lead) and
   the global cap refuse politely and write a security event.
6. With `TURNSTILE_SECRET_KEY` set, a missing or invalid token refuses the submit; a valid one (fake siteverify)
   passes.
7. `spam` triage → status `suppressed`, no email, included in the digest.
8. An SMTP outage → lead stored, 3 attempts, then `failed` + an event. The visitor's reply was already sent.
9. A pending draft survives a Redis flush and an API restart (checkpointer), and is pruned after 24 h.
10. `POST /v1/opportunities` with `leads:write` runs the same pipeline. `public:read` alone gets 403.
11. Admin list/patch work and are audited. `show_leads` in intake lists recent leads.
12. Injection inside opportunity fields ("ignore previous instructions…") is handled by the input guard as today
    and never reaches the email unescaped.
13. `make check` passes. Scripted conversations use fake models, fake SMTP and fake Turnstile. A live run sends
    one real email when the owner has configured SMTP, otherwise a local debug SMTP server; the PR states which.

## Open questions
None blocking. The owner provides a Gmail app password (Google Account → Security → App passwords) in `.env`,
never in git.
