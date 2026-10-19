# Spec: ingestion

Status: draft, awaiting approval · Owner: Surya Prakash Giri · Ticket: none (personal repo; `tasks.md` is the task list)

## Problem
The vault has storage, auth and a model layer, but nothing goes into it yet. The owner wants to add knowledge
in several forms:

- typed notes: interview experiences, applications, issues solved, features built
- files: documents, spreadsheets, Markdown, HTML, images and screenshots
- links: profiles, blog posts, public Google Docs
- personal GitHub repositories
- the LinkedIn data export

Every item must be parsed, cleaned of identifiers, labelled with a tier, embedded and held for review. Nothing
becomes public until the owner approves it, nothing sensitive is sent to a cloud model on the way, and no
employer-internal material slips in unnoticed.

## Goals
1. **Override wiring** (deferred from kb-storage): a MongoDB-backed `OverrideStore` adapter feeds the
   orchestrator. The admin override routes take effect on the next call with no restart.
2. **Admin ingestion routes** (vault profile, `admin` scope), all asynchronous. Each returns `202` with
   `source_id`; status is polled at `GET /admin/v1/sources/{id}`.
   - `POST /admin/v1/ingest/text`: title, body, category, tags, `occurred_at`, optional tier hint.
   - `POST /admin/v1/ingest/files`: multipart batch up to `INGEST_MAX_FILES` files and `INGEST_MAX_FILE_MB`
     each. Allowed: pdf, docx, xlsx, csv, md, txt, html, pptx, png, jpg, jpeg, webp. The type is detected from
     content (magic bytes), not the extension.
   - `POST /admin/v1/ingest/urls`: list of http(s) URLs. Public Google Docs/Sheets links are rewritten to their
     export URL.
   - `POST /admin/v1/ingest/github`: repo names from the allowlist `GITHUB_SYNC_REPOS` (owner's personal
     account only). Pulls README, description, topics, languages and last push.
   - `POST /admin/v1/ingest/linkedin-export`: the LinkedIn "Get a copy of your data" ZIP. Profile, positions,
     education, skills, certifications and projects are parsed into documents; messages and connections are
     ignored.
3. **Parsing:**
   - pymupdf4llm for PDF; markitdown for docx/xlsx/csv/pptx/html/md; plain text as is.
   - RapidOCR for images and for PDFs whose pages have no text layer.
   - Every source is stored first in the encrypted blob store (raw bytes), then parsed.
4. **Chunking:** Markdown-aware splitting that keeps heading context in each chunk. Target 400 tokens with 60
   overlap, both configurable. One document per source, except the LinkedIn export and GitHub, which produce
   one document per entity.
5. **Redaction before anything is stored as text or sent to a model.** Presidio (spaCy `en_core_web_md`) plus
   custom recognisers for PAN, Aadhaar, UAN, IFSC, bank account, passport, roll/enrolment numbers and dates of
   birth.
   - Matches are replaced with typed placeholders, e.g. `[AADHAAR]`.
   - The owner's published contact details (`PUBLIC_CONTACT_ALLOWLIST`) are kept.
   - A per-document redaction report (counts by type, no values) is stored.
6. **Employer guard:** an owner-maintained denylist at `vault/config/denylist.yaml`, git-ignored and never in
   the repo. It holds regexes and literal terms: internal domains, ticket-key patterns, internal repo or
   service names. A match puts the document into `quarantined`, with matched rule names only (no matched text
   shown in logs). Quarantined documents are not embedded until the owner releases or deletes them.
7. **Labelling** proposes a tier and a reason. The order is:
   1. Source and category rules. For example, `compensation` → confidential; `id-document` and `marksheet`
      files → restricted (blob only); `resume` and `certification-name` → private, with a public suggestion.
   2. Redaction results: identifiers found → at least private.
   3. The orchestrator's `classifier` role at **tier=confidential**, so only local models (Ollama/mock) ever
      see unlabelled text. With no local model available, the rule label stands and is marked `by: rules`.
   - The document stays `review_status: pending` and `published: false`.
   - The final tier is the **maximum** of the rule tier and the classifier tier; a model can raise a tier, never
     lower it.
8. **Restricted handling:**
   - A restricted document keeps its encrypted blob and a metadata-only record: title, category, date, and a
     short owner-written summary if one is given.
   - It produces no chunks and no vectors, so no model ever sees it.
9. **Embedding and indexing:**
   - fastembed `BAAI/bge-small-en-v1.5` (dense, 384) plus `Qdrant/bm25` (sparse), computed locally.
   - Chunks are upserted to the collection for their tier (`kb_private` or `kb_confidential`) with payload
     `document_id`, `tier`, `category`, `published=false`.
   - Re-ingesting identical content (same checksum) is a no-op that returns the existing `source_id`.
10. **Canaries:** each private or confidential document gets one canary chunk with a random marker, stored with
    `canary=true` and registered in a `canaries` list. The public output guard (public-agent) treats any
    canary seen in a public answer as a critical incident.
11. **Review and publish:**
    - `GET /admin/v1/review`: pending and quarantined items, newest first, with proposed label and reason.
    - `PATCH /admin/v1/documents/{id}`: owner sets tier, category, tags or title; re-labelling moves the vectors
      to the matching collection.
    - `POST /admin/v1/documents/{id}/approve`: marks it approved at its current tier.
    - `POST /admin/v1/documents/{id}/publish` requires tier `public` and approved. It copies the chunks (never
      canaries) to `kb_public` with `published=true`. `unpublish` removes them.
    - `DELETE /admin/v1/documents/{id}`: soft delete, removing its vectors from every collection.
    - Every write is audited.
12. **Profile and records:**
    - `GET/PUT /admin/v1/profile`: availability, notice period, preferred roles, locations, and which resume
      document/blob is the downloadable one.
    - `GET/POST/PATCH/DELETE /admin/v1/records`: applications and interviews (company, role, stage, date,
      notes) as structured data, not RAG.
13. **Jobs:** an in-process async worker pool (`INGEST_WORKERS`, default 2) works through a queue. Status moves
    `queued → parsing → redacting → labelling → embedding → done | quarantined | failed`. Failures store an
    error code and message, never document content. Jobs interrupted by a restart are re-queued at startup.

## Non-goals
Retrieval and answering (owner-agent), the public collection sync to the cloud (deploy-public), Google Drive
OAuth for private Docs, voice notes, scheduled re-crawls of URLs.

## Acceptance criteria
1. An override PUT changes the orchestrator's next selection without a restart.
2. Each supported type parses into non-empty Markdown. An unsupported or mislabelled type gets a `VALIDATION_ERROR`
   naming the detected type; an oversized file gets a 413-mapped error.
3. A fixture with fake PAN/Aadhaar/account/DOB values is stored with placeholders only. Neither chunk text, the
   Qdrant payload, logs nor the classifier input contains the raw values. The allowlisted public email
   survives.
4. A document matching the denylist is `quarantined`, has no vectors, and is listed in review with rule names.
5. The classifier is invoked with `tier=confidential`. With only cloud models enabled, no classifier call is
   made and the label is `by: rules`.
6. The final tier never ends up lower than the rule tier.
7. A restricted document has a blob, no chunks and no vectors.
8. Re-ingesting the same bytes returns the same `source_id` and creates nothing new.
9. Publish refuses non-public or unapproved documents. A published document has its non-canary chunks in
   `kb_public`. Unpublish and delete remove them, and delete also removes private vectors.
10. URL ingestion refuses non-http(s) schemes, private/loopback/link-local addresses (including after DNS and
    redirects), responses over the size cap, and more than 5 redirects.
11. GitHub ingestion refuses repos outside `GITHUB_SYNC_REPOS`.
12. A job killed mid-way is re-queued at startup and finishes once.
13. `make check` passes. Parsers, redaction and the pipeline are tested with fake-persona fixtures. Qdrant/Mongo
    round-trips are `integration` tests.

## Open questions
None blocking. Default category list (editable later): `resume`, `project`, `experience`, `education`,
`certification`, `skill`, `interview`, `application`, `work-note`, `compensation`, `marksheet`, `id-document`,
`profile-link`, `blog`, `persona`.
