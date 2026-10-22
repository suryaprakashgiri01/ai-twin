# Plan: answer-studio

## Architecture
```
make chat ─▶ owner graph: route ─▶ retrieve (narrow | broad: cards first) ─▶ generate (owner-v3, tier → ollama-qwen for confidential)
make prep ─▶ prep graph: classify type ─▶ sub-queries ─▶ retrieve (cards + chunks) ─▶ compose (first person) ─▶ outward filter ─▶ answer + sources + checks
ingestion ─▶ … redact (owner names kept) ─▶ guard ─▶ chunk ─▶ label ─▶ card (doc tier) ─▶ embed
kb_sync ─▶ admin API (ingest / refresh / release local_only / delete) per manifest entry; state.json
```

## Key decisions
- **Cards, not bigger chunks.** One summary per document answers "what are all my projects" without blowing the
  context budget, and it is generated where the document's tier allows.
- **Prep is a separate graph.** Persuasive first-person writing with general knowledge must not leak into the
  factual owner chat.
- **The filter runs on every prep answer.** Answers leave the vault by design, so internal identifiers are removed
  in code, not by prompt alone.
- **`local_only` is a document flag** checked by publish, so no path can publish it.
- **The loader is committed code over git-ignored data.** It is reusable on the Windows laptop, and the repo
  holds no personal data.

## Risks
- 7B local speed on CPU: the local timeout and an 8k context; prep shows progress.
- Card generation cost at ingestion: generated once per document; backfill is batch and resumable.
