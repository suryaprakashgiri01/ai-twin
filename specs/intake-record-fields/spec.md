# Spec: intake-record-fields

Status: approved via plan · Owner: Surya Prakash Giri · Ticket: none

## Problem
The owner describes job-search steps in plain language, including notes, location, work mode
(remote / onsite / hybrid), and other attributes. Intake drops anything outside the fixed field
list, ignores labelled cues (`Role:`, `notes:`, `in notes …`), fails on common role phrasings
("applied to the X role at Y", titles with `–` or `&`), and always suggests "Anything else from
this round?" even after an application. Pasted JSON is not accepted as a first-class input.
Title refresh often times out on a slow local model because the CLI forces an 8 s wait.

## Goals
1. **Record fields** on every record kind: `location` (free text), `work_mode`
   (`onsite | remote | hybrid`), `company_type` (`product | service | research | startup |
   other`, also from "product-based" / "service based"), `extras` (at most 10 keys of 200
   characters each). Optional; no migration.
2. **Owner-words parsing** before the model (`app/agents/intake/labels.py`): labelled cues win
   over the model and skip `ground`. Unknown labelled keys go into `extras`.
3. **Pasted JSON:** a message that is (or contains) a JSON object is validated against the
   action's API model. Valid → draft as given (snake_case or camelCase). Invalid → field errors.
4. **Role and location fallbacks:** "applied to (the) X role at Y", en/em dash and `&` / `II` in
   titles; location and work-mode cues from the owner's words.
5. **Suggestions by kind:** applications ask about referral / salary / location; interviews keep
   round wording.
6. **Prompt `intake-v4`:** rules and examples for location, work mode, notes and extras, plus
   the pydantic JSON schemas for `IntakeDraft` and `RecordIn` (`model_json_schema()`) so the
   model sees the full output shape and each field's meaning. Batch prompts also get the
   `RecordsBatchIn` schema (`records`: a list of `RecordIn`).
7. **Local title wait:** `TITLE_REFRESH_WAIT_LOCAL_SECONDS` (default 20) when the conversation
   tier forces the local model. The CLI stops sending a fixed wait and lets the server choose.
8. **Several records in one message:** the owner can list multiple applications (or other
   kinds) in one prompt ("today I applied to the following…", one line per role/company).
   Code splits them into a batch (`add_records`). Shared cues (date, kind, company_type)
   apply to every line. One confirmation saves every complete item; incomplete items are
   named and asked for before anything is written. A JSON array of record objects is
   accepted the same way.
9. **Batch API:** `POST /admin/v1/records/batch` with body `{ "records": [ RecordIn, … ] }`
   (1–50 items) creates them in one call, same validation and audit as a single create.

## Non-goals
API envelope / UUID / pagination (PR 2), graphify / ollama tooling (PR 3), docs rewrite (PR 4),
public-agent improvements (F8).

## Acceptance criteria
1. Saving a record with `location`, `work_mode` and `extras` via API and via intake keeps all
   three on the document and in list/get responses.
2. Labelled values (`Role: …`, `notes: …`, `in notes …`, `location: …`, `work mode: …`,
   `Job ID: …`) appear in the draft even when the model omits or paraphrases them.
3. A valid JSON paste for `add_record` becomes a confirmable draft without re-asking known
   fields; an invalid paste surfaces field errors.
4. The three Windows-log phrasings (role with en dash / `&`, `Role:` label, notes cue) produce
   complete drafts in unit fixtures without inventing fields.
5. After `add_record` of kind `application`, the suggestion mentions application extras, not
   "this round".
6. Title refresh with no `wait_seconds` from the client uses the local wait when the
   conversation is local-only; the CLI no longer hard-codes 8 s.
7. Existing intake and record tests still pass; new tests cover labels, JSON paste, fixtures,
   per-kind suggestions and the local wait setting.
8. A multi-line "I applied to r1 at c1 … / r2 at c2 …" message becomes one `add_records`
   draft with both rows; "yes" creates two records and nothing if the owner says no.
