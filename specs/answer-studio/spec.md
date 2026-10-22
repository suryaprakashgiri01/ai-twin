# Spec: answer-studio

Status: approved · Owner: Surya Prakash Giri · Ticket: none (personal repo; `tasks.md` is the task list)

## Problem
Owner mode should work as the owner's twin and personal assistant:
- answer interview questions (behavioural and technical scenarios) the way the owner would
- draft application-form answers
- summarise projects and achievements from everything in the vault

Instead, the owner chat pastes source snippets or replies "not in the vault":
- **One confidential chunk forces a 3B local model**, which echoes the context.
- **The prompt invites quoting** rather than synthesis.
- **Overview questions** get 6 chunks with no per-document summaries.
- **Follow-ups lose the previous exchange** to tier filtering.
- **The owner's own name** is redacted to `[PERSON]`.
- **Quarantined documents** can't be used locally at all.
- **GitHub repos** are all labelled public, and URL/GitHub sources never refresh.
- **Building the vault from local documents** takes many manual calls.

## Goals
1. **Quality local model.** `ollama-qwen` (`qwen2.5:7b`, generator + judge, `max_tier: confidential`, tags
   `local`, `local-quality`) sits before `ollama-local` in the generator and judge chains, so confidential
   answers use it.
   - The model override becomes per entry: `OLLAMA_MODEL` → `ollama-local`, `OLLAMA_QUALITY_MODEL` →
     `ollama-qwen`.
   - `prefer` accepts `local-quality`.
   - The Ollama script and `doctor` handle every Ollama entry.
2. **Owner name kept.** `OWNER_NAMES` (comma-separated variants; default `PUBLIC_OWNER_NAME`) is never redacted.
   A PERSON span made only of owner-name tokens is kept.
3. **Owner chat answers.**
   - Prompt `owner-v3`: synthesise in plain sentences, structure summaries, cite inline, never paste the context
     back.
   - **Document cards:** a ≤ 800-character summary per document, generated at ingestion at the document's tier
     and indexed as a `card` point. A backfill command covers existing documents.
   - **Broad mode** for overview questions: cards first, more results, a larger context, grouped by document.
   - **Follow-ups:** a short follow-up whose own retrieval is empty reuses the previous turn's cited documents.
     History visibility follows the conversation's eligible tier.
4. **Local-only release.**
   - A quarantined document can be released `local_only`: indexed at confidential, approvable, never
     publishable while the flag is set.
   - Available through the API (`localOnly`, default true for quarantined documents) and the intake (`release`).
     Audited.
5. **Prep agent** (`POST /admin/v1/prep`, SSE; `make prep`). Answers in the owner's first-person voice.
   - **Types:**
     - `behavioural` (STAR with role and measurable impact)
     - `scenario` (numbered steps, plus examples from the owner's work)
     - `application` (within a word or character limit)
     - `pitch`
     - `project`
   - **Retrieval:** sub-queries over cards and chunks, up to confidential.
   - **Claims:** personal claims and numbers only from sources; an unsupported number becomes `[add metric]`.
     General engineering knowledge is allowed for scenario steps.
   - **Outward-safety filter, always on:** internal identifiers (employer denylist, repo/service names, topics,
     cache keys, internal URLs, ticket keys) are rewritten generically and listed.
   - **Output:** answer, sources used, and a "check before sending" list.
   - **REPL:** `/shorter`, `/longer`, `/variant`, `/words N`, `/chars N`, `/mode X`.
6. **KB loader.**
   - `scripts/kb_sync.py` (`make kb-sync`) reads a git-ignored `vault/kb/manifest.yaml` and `vault/kb/docs/`.
   - Dry run by default; `--apply`; idempotent through `vault/kb/state.json`; a changed entry replaces its
     documents.
   - `--reset` exports a backup, then removes only knowledge-base data.
   - URL and GitHub ingest accept `refresh=true`.
   - A GitHub repo's tier follows its visibility; private repos need `GITHUB_TOKEN`.

7. **Super agent** (`POST /admin/v1/super`, SSE; `make super`): one local-only conversation that
   plans each request and delegates to the other agents. You never have to switch agents.
   - **Sub-agents:** owner chat (vault and records questions, all tiers), prep (interview and
     application answers), intake (notes, records such as today's interviews, invites,
     applications, ingestion, review actions).
   - **Plan, run, compose:**
     - A planner model splits the request into at most four steps, each for one sub-agent.
     - The steps run in order; one step's output can feed the next ("add today's interview,
       then prep me for it").
     - A composer writes one answer from the step results.
   - **Writes:** intake drafts that only add or update data are applied at once. Deleting,
     publishing or unpublishing still waits for a yes.
   - Everything runs on the owner's machine, at `max_tier` confidential. The tier rule decides
     which models may read each step.
8. **Per-agent models** in `config/agents.yaml`:
   - Each agent has a `model` (`auto` or an inventory id) and a `confidential_model` (`auto` or
     a local inventory id).
   - **auto:** the role chain picks the model, with the agent's `prefer`.
   - **Pinned:** a pinned id is used whenever its `max_tier` allows the step's tier. Otherwise
     the agent falls back to auto, so confidential text still goes to local models only.
   - **Per request:** the request's `model` overrides both.
   - The super agent's confidential model is `ollama-qwen-14b` (`qwen2.5:14b`, ~9 GB; it fits
     a 24 GB Mac next to the stack).
9. **Paid models** can be added to the inventory:
   - `anthropic` and `openai` providers; the `openai` provider also takes `base_url` for any
     OpenAI-compatible service.
   - Keyed by `ANTHROPIC_API_KEY` and `OPENAI_API_KEY`; disabled until the owner enables them.
   - A model on a cloud provider may never declare `max_tier` above private. The inventory
     refuses to load if one does.

## Non-goals
Public-agent changes, cloud models for confidential text, publishing `local_only` documents, storing any owner
data in git.

## Acceptance criteria
1. With confidential context, the generator is `ollama-qwen` when available, falling back to `ollama-local`.
   With `OLLAMA_MODEL` set, `ollama-qwen` still uses its own model.
2. The owner's name survives redaction; other names are still redacted.
3. A KB answer never starts with a pasted `[n] title` block (asserted on the prompt and on mock output handling).
   Overview questions use cards and a larger budget. A short follow-up with empty retrieval reuses the previous
   citations.
4. A quarantined document released `local_only` is retrievable by the owner, approvable, and refused by publish.
5. Prep answers are first person; types are detected; limits are enforced. The filter removes internal
   identifiers from a fixture and lists them. Confidential sources mean local models only.
6. The loader's dry run changes nothing; `--apply` is idempotent; a changed file replaces its document;
   `--reset` keeps keys, profile, records, leads and conversations; `refresh=true` replaces a URL source's
   documents; private repos are labelled private.
7. The super agent answers a mixed request ("I have an interview with Acme today at 3 pm for an AI
   Engineer role; add it and give me a 60-second pitch for it") with one record added through
   intake and one prep answer. A delete still waits for a yes. Confidential steps use local models.
8. `config/agents.yaml` pins and auto mode resolve as described; a pinned cloud model never
   receives a confidential step.
9. A cloud-provider entry with `max_tier: confidential` fails inventory validation; paid entries
   are unavailable without their key.
10. `make check` passes; a live macOS run is reported in the PR.
