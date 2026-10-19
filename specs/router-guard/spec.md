# Spec: router-guard

Status: draft · Owner: Surya Prakash Giri · Ticket: none (personal repo; `tasks.md` is the task list)

## Problem
The owner graph trusts the router model's structured `RouteDecision`. A small local router (Ollama
`llama3.2:3b`) gets follow-ups wrong in a way the keyword fallback would not. Observed on the real stack:
after a KB answer about a job offer, the follow-up "and which other company am I weighing it against?" was
routed to `records`, rewritten to "which companies", and given filters nobody asked for (a company, an
`open` stage, a whole-year date range). The records tool then answered "You have 0 applications…", which is
exact for the invented query and wrong for the question.

## Goals
1. **Records route must be earned by the question.** A model `records` route is accepted only when the
   owner's own words look like a counting or dated records question: `looks_like_records()` on the question,
   with a short follow-up folded into the previous user question (`standalone()`, the same text the keyword
   router uses). The model's rewritten query is not used for this check, because the rewrite is the part that
   was invented. When the check fails, the keyword router decides the turn (route and query).
2. **Filters must be mentioned.** On an accepted model `records` route, `company` and `stage` filters whose
   value does not appear in that same text (case-insensitive) are dropped.
3. **Metadata-only logging.** The `route` step log gains `guard` (`none`, `records_rejected`,
   `filters_dropped`) and `dropped` (filter names only). No question, query or filter value is logged.

## Non-goals
- **Retrying KB search when a records answer is 0** (candidate (c)). Zero is a correct, common answer to
  "how many interviews this week?"; replacing it with a KB answer would hide it. Goal 1 already removes the
  observed case, and the graph keeps its existing one-way KB-miss → records fallback, so no loop is added.
- Validating model date ranges. Dates are rarely mentioned word-for-word ("in 2026", "since the summer"),
  so dropping them would break correct routes; goal 1 covers the observed invented dates.
- Prompt changes for the router; heuristic-route decisions are unchanged.

## Acceptance criteria
1. A model `records` route for "and which other company am I weighing it against?" after a KB turn about an
   offer becomes `kb_search`; the KB is searched with the owner's question, and the records tool is not called.
2. A model `records` route for "how many interviews in September?" is unchanged (existing tests still pass).
3. A short follow-up such as "and in August?" after a records question keeps the model's `records` route.
4. Model filters `company="Globex Labs"` / `stage="open"` on "how many interviews this month?" are dropped;
   a company named in the question ("how many interviews with Globex Labs?") is kept.
5. The `route` log line carries `guard` and `dropped` and contains no question or filter text.
6. Re-run on native Ollama with `llama3.2:3b` (`LLM_MOCK_ENABLED=false`): the follow-up in AC1 routes to
   `kb_search`.
