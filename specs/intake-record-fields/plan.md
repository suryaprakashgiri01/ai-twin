# Plan: intake-record-fields

## Approach
Extend the existing record model and intake gather path. Label parsing and JSON paste run in
gather *before* the model extract / ground / merge chain so owner-labelled and pasted values
always win. Fallbacks for role, location and work mode stay in `app/agents/graphs/intake.py`
next to the existing company/role regexes. Suggestions branch on `outcome["record"]["kind"]`.

## Files
- `app/db/documents.py` — `WorkMode`, Record fields
- `app/services/profile.py` — `RecordIn` / `RecordPatch` / validation for `extras`
- `app/agents/intake/actions.py` — field lists, `_record_line`
- `app/agents/intake/labels.py` — new: label + JSON paste parsers
- `app/agents/intake/drafts.py` — allow `extras` through normalise; merge labelled keys
- `app/agents/intake/suggest.py` — per-kind tips
- `app/agents/graphs/intake.py` — wire labels/JSON, role/location/work_mode fallbacks
- `app/agents/prompts/intake.py` — `intake-v4`
- `app/core/config.py`, `app/api/routes/admin_chat.py`, `scripts/chat.py` — local title wait
- `tests/intake/`, `tests/agent/test_record_kinds.py` — new coverage
- `dev-notes/testing/intake-record-fields.md` — live cases (git-ignored)

## Risks
- `extras` must not swallow known fields; labelled known keys take precedence.
- JSON paste must not treat arbitrary prose braces as JSON; require a top-level object.
- camelCase aliases in paste only for this PR's ingest models; full camelCase is PR 2.
