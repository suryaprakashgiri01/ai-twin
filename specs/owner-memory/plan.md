# Plan: owner-memory

## Architecture
```
turn appended ─▶ every SUMMARY_EVERY_TURNS ─┐
/exit ─────────▶ POST …/title (existing) ───┴▶ ConversationDigester (one structured call, conversation tier)
                                                 └▶ {title, description, summary, memory_candidates[]}
                                                       ├▶ conversation: name/description/summary/summarized_turns
                                                       └▶ MemoryService.learn(candidates): mask → filter → dedupe → pending|bump
owner chat / intake prompt build ─▶ ContextBuilder: [Owner preferences (active, tier-filtered)] + [summary if long] + recent turns
make memory · /remember · /memory · intake remember/forget/show_memory · /admin/v1/memories ─▶ MemoryService (audited)
public agent ─▶ (no memory dependency at all)
```

## Module layout

| Module | Responsibility |
|---|---|
| `app/db/documents.py` | `Conversation.summary`, `summarized_turns`; `OwnerMemory` document + indexes |
| `app/migrations/m0006_owner_memory.py` | defaults for existing conversations; memory indexes |
| `app/services/titling.py` → `digest.py` | One structured call returning title, description, summary and candidates (`digest-v1`); periodic and on-exit triggers |
| `app/services/memory.py` | Store, learn (mask, filter, dedupe via fastembed similarity), explicit add, accept/edit/archive/delete, budgeted selection by tier |
| `app/agents/context.py` | Shared prompt blocks for owner chat and intake: preferences + summary |
| `app/agents/graphs/owner.py`, `app/agents/graphs/intake.py` | Use `context.py` when building prompts |
| `app/agents/intake/actions.py` | `remember`, `forget`, `show_memory` actions |
| `app/api/routes/admin_memory.py` | Memory CRUD; `GET /admin/v1/conversations/{id}` returns `summary` |
| `scripts/memory.py`, `scripts/chat.py` | `make memory`; `/remember`, `/memory`; summary in the continue banner; pending notice on exit |
| `scripts/_vault.py` | Include `owner_memories` in export/import |

## Key decisions
- **One call per refresh.** Title, summary and memory candidates come from the same transcript at the same
  tier, which costs one local-model call on exit instead of three.
- **Pending first.** Learned preferences change agent behaviour, so the owner approves them. Things the owner
  says explicitly are active at once.
- **Preferences, not facts.** Facts belong in the vault, where review, tiers and publishing already apply.
  Memories stay small, human-readable and safe to inject.
- **Owner messages only.** The assistant's own words can't become "preferences", which closes a loop where the
  model teaches itself.
- **Tier on every memory.** A preference learned in a confidential chat never reaches a cloud model.
- **Designed from first principles for this repo.** Nothing is taken from employer codebases. This repo is
  public and personal, and the owner's rules forbid it.

## Data model
- `owner_memories`: `_id`, `kind`, `text`, `status`, `tier`, `confidence`, `source_conversation_id?`,
  `created_at`, `updated_at`, `uses`, `norm` (normalised text, unique among non-archived).
- Indexes: `status`+`kind`, `norm` (partial unique).
- `conversations` gains `summary?`, `summarized_turns` (default 0).

## Risks
- **A small local model producing weak summaries or candidates.** The schema is strict and code filters
  candidates; pending-first keeps bad ones out of prompts.
- **Prompt bloat.** Hard token budgets; the summary is only added when history is longer than the window.
- **Background summarising competing with chat on a CPU laptop.** It runs after the turn is stored, never
  blocks the reply, and is skipped while another summary is running for the same conversation.
