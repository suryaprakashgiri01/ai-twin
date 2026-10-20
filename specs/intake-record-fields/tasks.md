# Tasks: intake-record-fields

Each task is one commit. Commit subjects reference the task id.

| ID | Task | Verifies AC |
|---|---|---|
| T1 | Spec / plan / tasks committed | — |
| T2 | Record model + API: `location`, `work_mode`, `extras` on document, RecordIn/Patch, action field lists, `_record_line` | 1 |
| T3 | Label parser + JSON paste (`labels.py`); wire into gather so labelled/pasted values win and skip ground | 2, 3 |
| T4 | Role / location / work_mode regex fallbacks; per-kind suggestions; prompt `intake-v4` | 4, 5 |
| T5 | Local title-refresh wait setting; CLI stops sending fixed wait | 6 |
| T6 | Unit tests (labels, JSON, Windows fixtures, suggestions, wait); `make check` | 7 |
| T7 | Multi-record batch: parse several applications in one message, `add_records`, one yes; `company_type`; `POST /records/batch` | 8, 9 |
| T8 | Live testing notes in `dev-notes/testing/`; test report; PR | 1–9 |
