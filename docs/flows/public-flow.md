# Public flow

A visitor message through `POST /v1/chat`. Derived from `build_public_graph` in
`app/agents/graphs/public.py` and the capture nodes in `app/leads/capture.py`; the service wrapper is
`app/services/public_chat.py`.

Isolation is in code: the retriever is built with `kb_public` as its only collection and every search runs at
`Tier.PUBLIC` with no filters. Nothing in the state, the request or a prompt can name a tier or a collection.

## Graph

Without lead capture configured, the graph has five nodes. With it (`PublicDeps.capture`), three more
(`capture`, `confirm`, `submit`) are added and the graph needs a checkpointer, because the opportunity draft
lives in the thread's state (`public:<conversation id>`) between turns.

```mermaid
flowchart TD
    START([START]) --> input_guard
    input_guard -->|"allowed"| scope
    input_guard -->|"blocked: intent=blocked, fixed reply"| END1([END])

    scope -->|"about_owner"| retrieve
    scope -->|"refusal, greeting, resume, availability: fixed or template reply"| END2([END])
    scope -.->|"capture on: opportunity, or a draft is open"| capture

    retrieve --> generate
    generate -->|"generated=true"| output_guard
    generate -->|"no context, or model failed: fixed reply"| END3([END])
    output_guard --> END4([END])

    capture -.->|"extract, ask, or exit"| END5([END])
    capture -.->|"question in between about the owner"| retrieve
    capture -.->|"draft complete"| confirm
    capture -.->|"explicit yes while in confirm stage"| submit
    confirm -.->|"interrupt, then resume with the visitor's next message"| input_guard
    submit -.-> END6([END])
```

Dashed edges exist only when capture is configured. Each node logs one `public step` line with counts, intent,
rule names and latency; no visitor text, context or answer is logged.

### Routing functions

| Function | Rule |
|---|---|
| `_after_input` | `intent == "blocked"` → END, else `scope` |
| `_after_scope` (capture off) | `about_owner` → `retrieve`, else END |
| `_after_scope_capture` (capture on) | a draft is open or intent is `opportunity` → `capture`; otherwise as `_after_scope` |
| `_after_generate` | `generated` → `output_guard`, else END |

### Nodes

| Node | What it does | Failure behaviour |
|---|---|---|
| `input_guard` | Length cap, normalisation, heuristic rules, Prompt Guard 2 score, optional Jev second opinion | Block writes an `input_blocked` security event and the fixed `BLOCKED_REPLY`; a guard-model outage is noted and the message continues |
| `scope` | Rules, then `FlaggedClassifier` at public tier, then keyword fallbacks. Fixed replies for `meta_probe`, `out_of_scope`, `action_request`, `opportunity`, `greeting`; profile templates for `resume` and `availability` | `meta_probe` records a low-severity event |
| `retrieve` | Hybrid search at `Tier.PUBLIC` over `kb_public`, then cross-encoder rerank to `top_k` | none; an empty result leads to the no-context reply |
| `generate` | Builds a tier-capped context, then one `Role.GENERATOR` call at public tier with up to 4 answered `about_owner` turns of history | Empty context → `NO_CONTEXT`; `OrchestratorError` → `UNAVAILABLE`; both skip the output guard because nothing was generated |
| `output_guard` | Canary, identifier, disclosure, grounding checks on the whole answer | A failed check replaces the answer with `NO_CONTEXT`, clears sources, and writes `canary_hit` (critical) or `output_blocked` |
| `capture` | Extracts opportunity fields; see below | Turn cap → fixed contact-form reply |
| `confirm` | `interrupt`; the visitor's reply returns to `input_guard` | none |
| `submit` | Stores the lead and queues the email | Failure, missing Turnstile verification or caps produce fixed replies |

Refusals end at `scope`: no retrieval, no generation. Because a generated answer is complete when it reaches
`output_guard`, nothing a visitor sees has skipped it.

## Delivery sequence

Delivery is buffered. The graph finishes, the output guard has passed (or replaced) the whole answer, and only
then do `token` events go out.

```mermaid
sequenceDiagram
    participant V as Visitor
    participant P as Portfolio server route
    participant A as Public API
    participant S as public_chat service
    participant G as Public graph
    participant Q as Qdrant kb_public
    participant L as llm-orchestrator
    V->>P: message
    P->>A: POST /v1/chat (service key, X-Visitor-Address)
    A->>A: auth scope public:read, rate limit
    A->>S: message, visitor hash
    S->>G: initial_state(...)
    G->>G: input_guard
    G->>G: scope
    alt about_owner
        G->>Q: hybrid search, public tier
        Q-->>G: candidate chunks
        G->>G: rerank, build context
        G->>L: chat(GENERATOR, PUBLIC, messages)
        L-->>G: answer
        G->>G: output_guard on the full answer
    else refusal, greeting, resume
        G->>G: fixed or template reply
    end
    G-->>S: final state
    S-->>A: SSE meta, token*, sources, done
    A-->>P: stream
    P-->>V: answer
```

Events, in order: `meta` (conversation id and intent), `token`*, `sources`, `done`. An unexpected failure ends
with `error`. Both turns are stored after the answer is decided, with the intent and guard verdicts (rule names
only) on the assistant turn. An answer cache short-circuits repeat questions and is invalidated when
publishing or unpublishing changes `kb_public`.

## Opportunity capture

The model only extracts. Email and link come from patterns in code, any model-provided value must be grounded in
the visitor's own words, and `app.leads.models.check` decides what is valid and what is missing. One short
question per turn, at most `LEAD_MAX_TURNS`, then the contact-form reply. Replies are fixed templates around the
visitor's own values: no generated text reaches the visitor from the capture nodes.

```mermaid
sequenceDiagram
    participant V as Visitor
    participant G as Public graph
    participant C as capture
    participant CF as confirm (interrupt)
    participant SB as submit
    participant LS as Lead service
    V->>G: "I have an opportunity"
    G->>G: input_guard, scope = opportunity
    G->>C: capture
    C-->>V: intro and first question (draft saved in checkpoint)
    loop until complete
        V->>G: answer
        G->>G: input_guard, scope
        G->>C: capture (draft open)
        C-->>V: next question
    end
    C->>CF: complete draft
    CF-->>V: summary, "send it?" (graph paused)
    V->>G: reply (resume)
    G->>G: input_guard, scope
    G->>C: capture (stage = confirm)
    alt explicit yes
        C->>SB: submit
        SB->>LS: submit(lead, turnstile_token, summary)
        LS-->>V: thanks (email queued)
    else change request
        C-->>V: back to gathering
    end
```

A question about the owner in the middle of capture is answered through `retrieve` as usual and the draft waits.
Nothing is stored or emailed before an explicit yes, and the Turnstile token is checked on submit only. The
optional summary for the owner passes the output guard or the email goes without it.
