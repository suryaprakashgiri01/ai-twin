# Plan: router-guard

## Where the guard sits
```
route node ─▶ _decide (model, else keywords)
           └▶ guard_records(decision, question, previous, today)   ← model decisions only
                 ├─ records route, question text not records-like ─▶ heuristic_route(...)  guard=records_rejected
                 ├─ records route, company/stage not in text       ─▶ filters cleared     guard=filters_dropped
                 └─ otherwise                                       ─▶ unchanged           guard=none
```

- `app/agents/routing.py`: `GuardResult` (decision, guard label, dropped names) and `guard_records()`. Pure
  function over the decision and the owner's text, so it is unit-testable without a graph.
- `app/agents/graphs/owner.py`: `route` computes the previous user question once, passes it to `_decide`, runs
  `guard_records` when the source is `model`, and logs `guard` / `dropped`.
- The text checked is `standalone(question, previous)`, so short follow-ups ("and in August?") inherit the
  previous question's records shape, exactly as the keyword router already does.

## Tests
- `tests/agent/test_router_guard.py`: pure `guard_records` cases plus graph runs through `make_agent` /
  `route_json` for AC1–AC5.
- Real re-run against native Ollama for AC6, recorded in the PR.
