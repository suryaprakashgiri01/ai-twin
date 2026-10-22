# Plan: llm-gateway

## Architecture
```
agent step ─▶ Need (caller | rules | Laya) ─▶ gateway.order(role, tier, need, prefer, pin)
                                               │ eligible: tier, availability, capabilities, not cooling off
                                               └ fit order: complexity → quality target, then cost, then chain
           ─▶ executor (per model): Ollama reasoning=True · strip <think> · 404 → cool-off
doctor / make models-check ─▶ provider /models ─▶ missing ids report
```

## Key decisions
- **Ordering, not routing.** The gateway reorders the existing eligible chain. Policy (tier caps,
  availability, overrides) stays in `selection.py`/`policy.py` and is applied first, so the gateway can never
  widen access.
- **Rules first, Laya second.** Rules are free and predictable; Laya adds a local, private judgement when the
  owner enables it. A failure never blocks a request: the rules' label stands.
- **Capabilities are declared, not probed.** The inventory is the source of truth; `models-check` verifies
  ids exist, not what the models can do.
- **The cool-off is in-process memory.** It is per process and expires on its own; no new store.

## Risks
- **Quality ranks are judgement calls.** They live in YAML, so the owner can tune them.
- **Laya latency on CPU** (about 150 ms per call on a Mac CPU): only used when enabled, and only for
  generator-role steps.
