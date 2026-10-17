# Plan: ingestion

## Architecture
Admin routes validate the input, store raw bytes in the blob store, create a `Source` and enqueue a job. Workers
run a fixed pipeline of small, separately testable stages. Each stage takes and returns a typed `IngestItem`, so
the order is explicit and tests can run one stage alone.

```
route ─▶ BlobStore.put ─▶ Source(status=queued) ─▶ queue
worker: fetch/parse ─▶ redact ─▶ employer guard ─▶ chunk ─▶ label ─▶ (restricted? stop) ─▶ embed ─▶ upsert(tier collection)
                                       └─▶ quarantined (stop)
review/publish routes ─▶ Qdrant copy/move/delete + KbDocument/Chunk updates + audit
```

## Module layout

| Module | Responsibility |
|---|---|
| `app/core/llm.py` (+ `app/services/model_overrides.py`) | `MongoOverrideStore` implementing the SDK `OverrideStore` |
| `app/kb/ingestion/models.py` | `IngestItem`, job status enum, error codes |
| `app/kb/ingestion/parsers/{pdf,office,text,image,dispatch}.py` | Content-sniffed parsing to Markdown |
| `app/kb/ingestion/fetch/{url,github,linkedin}.py` | SSRF-safe fetch, Docs export rewrite, GitHub allowlist, LinkedIn ZIP |
| `app/kb/ingestion/redaction.py` | Presidio analyzer + Indian-ID recognisers + allowlist; report |
| `app/kb/ingestion/guard.py` | Denylist loader and matcher (rule names only) |
| `app/kb/ingestion/chunking.py` | Markdown-aware splitter with heading context |
| `app/kb/ingestion/labelling.py` | Rules table, classifier call at confidential tier, max-tier merge |
| `app/kb/embedding.py` | fastembed dense + sparse, lazily loaded, shared |
| `app/kb/ingestion/pipeline.py` + `jobs.py` | Stage runner, async worker pool, restart recovery |
| `app/kb/publishing.py` | approve, publish, unpublish, relabel move, delete |
| `app/kb/canaries.py` | Canary generation and registry |
| `app/api/routes/admin_ingest.py`, `admin_review.py`, `admin_profile.py`, `admin_records.py` | Routes |

## Key decisions
- **Classifier runs at `confidential`.** Unlabelled text is treated as the most sensitive tier a model may see.
  That forces local models, and the existing tier policy enforces it, so no new mechanism is needed.
- **Redact before guard, chunk and label.** Nothing downstream, models or logs included, ever holds raw
  identifiers. The blob keeps the original for the owner.
- **Max-merge of tiers.** The model can only make a document more private.
- **Vectors live in the collection for their tier.** Publishing copies to `kb_public`, and private copies stay
  for owner retrieval. A relabel is a delete-and-upsert between collections.
- **In-process workers.** Volume is personal-scale. Job state in MongoDB makes restarts safe without a broker.
- **Heavy dependencies stay in the vault.** Presidio/spaCy, RapidOCR and pymupdf go in a `vault` dependency
  group, so the future public image stays lean.

## Data model changes
- `Source.status` gains the job states; new fields `error_code?`, `attempts`, `updated_at`.
- `KbDocument` gains `quarantine_rules[]`, `redaction_report{type: count}`, `summary?`; `review_status` gains
  `quarantined`.
- New `canaries` collection: `document_id`, `marker` (unique), `created_at`.
- `Profile` gains `resume_document_id?`.
- One migration adds the fields and indexes.

## Risks
- **spaCy model size (~40 MB) and first-run download.** `setup` fetches it once; `doctor` checks for it.
- **OCR quality on screenshots varies.** The parsed text is shown in review so the owner can edit it.
- **fastembed first-run model download.** It is cached in `vault/models`, which is excluded from export.
- **LinkedIn export format changes.** The parser is tolerant: unknown CSVs are skipped and logged by name.
