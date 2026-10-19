# Plan: owner-intake

## Architecture
```
make twin / POST /admin/v1/assistant
  └▶ IntakeService (ConversationService history + MongoDB checkpointer, tier=confidential)
       intake graph:
         gather ──(structured IntakeDraft; code validates + lists missing)──▶ ask user ─▶ END turn
            └ complete ─▶ confirm (interrupt: summary + JSON) ─▶ resume with owner reply
                              ├ no ─▶ discard
                              ├ edit ─▶ gather
                              └ yes ─▶ (delete? second confirm) ─▶ execute (AdminOps) ─▶ report + suggestion
         answer ─▶ owner chat graph (read-only)
admin routes ──────────────────────────────────────▶ AdminOps (same methods, same audit)
```

## Module layout

| Module | Responsibility |
|---|---|
| `app/services/admin_ops.py` | Service layer extracted from admin routes; actor-aware audit |
| `app/api/routes/admin_*.py` | Become thin: parse, call `AdminOps`, shape the response |
| `app/agents/intake/actions.py` | Action catalog: name → pydantic input model, `AdminOps` call, destructive flag, summary formatter |
| `app/agents/intake/drafts.py` | `IntakeDraft`, missing-field computation, date normalisation, inbox path resolution |
| `app/agents/intake/confirm.py` | Yes/no/edit classification (rules, then model) and second-confirmation check |
| `app/agents/intake/suggest.py` | Deterministic next-step suggestions |
| `app/agents/graphs/intake.py` | State, nodes, interrupt, graph build |
| `app/agents/checkpoint.py` | MongoDB checkpointer (library saver if it supports PyMongo async, else a small Beanie-backed saver) + pruning |
| `app/services/intake.py` | Glue: history, graph run and resume, SSE events, persistence |
| `app/api/routes/admin_assistant.py` | `POST /admin/v1/assistant` |
| `scripts/chat.py` | `twin` mode, MODE column, resume at a pending draft |

## Key decisions
- **Code decides completeness, the model only extracts.** Small local models are unreliable at "is this
  complete?". Pydantic validation and required-field lists are exact, so a bad extraction costs one extra
  question, never a wrong write.
- **Reuse the input models the routes already validate with.** One definition of a valid record or note.
- **Confirm with `interrupt` + checkpointer, not by re-asking the model.** The pending draft is data, so it can't
  drift or be hallucinated between turns.
- **Deterministic suggestions.** Predictable, testable, and never a write by themselves.
- **Local-only by tier.** Running at `confidential` reuses the orchestrator's policy; no new switch can send
  owner text to a cloud API.

## Data model changes
`Conversation.mode` (`ask` default, `intake`). New `checkpoints` (and `checkpoint_writes` if the saver needs
them) collections, keyed by conversation id, with an `updated_at` index for pruning. Migration `0004` sets
`mode: ask` on existing conversations.

## Risks
- **LangGraph MongoDB saver vs PyMongo async.** Check at build time; fall back to a small saver implementing
  `BaseCheckpointSaver` on our client.
- **`dateparser` ambiguity** ("12/10"). Use the `DMY` order for India and always show the resolved date in the
  draft.
- **The extraction refactor could change route behaviour.** T1 runs the existing route tests untouched as the
  guard.
