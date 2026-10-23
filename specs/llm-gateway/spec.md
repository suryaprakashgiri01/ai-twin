# Spec: llm-gateway

Status: draft · Owner: Surya Prakash Giri · Ticket: none (personal repo; `tasks.md` is the task list)

## Problem
Model choice today is a fixed chain per role, reordered only by an agent's `prefer` tag:
- **Simple and hard requests get the same order.** A one-line routing question and a multi-project summary
  walk the same chain. Easy steps waste the strongest model; hard ones may land on a small one.
- **Models have no declared capabilities.** Nothing says which can use tools or reason step by step, so an
  agent can't ask for "reasoning" and get a model that does it.
- **Reasoning models leak their thinking.** Qwen3 and gpt-oss-style models return `<think>` text that would
  appear in answers.
- **Stale model ids fail silently.** When a provider retires an id (`llama-3.1-8b-instant` now returns 404),
  every request pays for a failed call before falling back. Nothing reports it.

## Goals
1. **Capabilities in the inventory.**
   - Each model declares `capabilities` (`text`, `tools`, `reasoning`; default `text`) and a `quality` from
     1 to 5 (a rough strength rank). Cost already exists.
   - Optional Qwen3 entries are added: `ollama-qwen3-8b` and `ollama-qwen3-14b`, with tools and reasoning.
     Both are disabled until pulled.
2. **A request profile (`Need`).**
   - Fields: `complexity` (`simple`, `moderate` or `complex`), `tools` (bool) and `reasoning` (bool).
   - A caller may set it. Otherwise a profiler derives it:
     - rules first: length, structure, and wording such as "step by step", "compare", "design", "why";
     - then Laya, when enabled, scores complexity locally, so it may read up to confidential text;
     - the rules decide when Laya is off or fails.
3. **Auto mode in the gateway.**
   - With no pin, the gateway filters the role's chain to models that are eligible: tier, availability, and
     the required capabilities.
   - It orders them by fit: complex → strongest first; moderate → quality 3 or more first; simple →
     cheapest adequate first. Chain order breaks ties.
   - Every eligible model stays as a fallback.
   - Agent `prefer` and pins keep working and win over auto ordering.
   - `config/agents.yaml` gains an optional `need` per agent: prep is complex, intake extraction moderate,
     routing simple.
4. **Reasoning output handled.**
   - Ollama models with `reasoning` run with thinking separated (`reasoning=True`).
   - For every provider, any remaining `<think>…</think>` is removed from the text before it reaches a graph.
   - Thinking is never part of an answer and never logged (only its size). It was never stored with a
     conversation; owner-console T3 now keeps it, capped, in the trace of owner turns only.
   - Owner channels (owner chat, prep, the super agent) stream it as a separate `thinking` event. The CLI
     shows it dimmed above the answer (`/thinking` toggles it), and an owner-mode UI can use the same event.
     The public channel never forwards it.
5. **Model-id drift.**
   - `make models-check` (and a `doctor` line) compares each enabled cloud model id with the provider's live
     model list (OpenAI-compatible `/models`, Anthropic and Gemini lists) and reports ids that no longer
     exist. Keys are never printed.
   - At runtime, a model that answers "model not found" (404) is skipped for 10 minutes instead of failing on
     every request, and a warning says which id to update.
6. **Observable choices.** One `gateway` log line per selection: the need, its source (caller, rules or
   Laya), the first three models tried, and why. No prompt text.

## Non-goals
Building tool-using agents (the capability is declared for later); sending confidential text to cloud models
(the tier cap is unchanged); training a classifier; automatic edits to `config/models.yaml`.

## Acceptance criteria
1. Inventory validation accepts `capabilities` and `quality` and rejects unknown capabilities. Existing entries
   load with defaults.
2. The profiler marks "If inference latency increases after new features, give me a step-by-step diagnosis"
   complex with reasoning, and "thanks" simple. With Laya enabled and scripted, Laya's label is used; when
   Laya fails, the rules' label is used.
3. For the same role and tier, a complex need tries a quality-5 model first and a simple need a cheaper
   adequate one. A need for `reasoning` tries models with it first; models without it stay only as
   fallbacks, so an unreachable provider never leaves a request without a model. Pins and `prefer`
   still win. Confidential needs still get local models only.
4. A scripted reasoning model's `<think>` text never appears in an answer, a stored turn or a log line. The
   owner chat emits it as a `thinking` event; the public channel does not.
5. `models-check` reports a retired id from a recorded provider list. After a 404 "model not found", the
   model is skipped on the next selection and tried again after the cool-off.
6. `make check` passes; a live run (Mac or laptop) is reported in the PR.
