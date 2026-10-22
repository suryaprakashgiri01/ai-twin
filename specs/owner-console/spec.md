# Spec: owner-console

Status: approved · Owner: Surya Prakash Giri · Ticket: none (personal repo; `tasks.md` is the task list)

## Problem
Every agent and every admin action is reachable only through the CLI or Postman. There's no single place to:
- watch an agent's plan, steps and reasoning;
- browse past conversations;
- switch models per agent;
- test the public agent as a visitor would.

Old conversations keep none of what the live run showed. On Windows, `make run` can't stop gracefully *and*
return a clean prompt.

## Goals
1. **Console.** Plain HTML/JS served by the app at `/ui` (`/` redirects there) on both profiles, under a strict
   CSP: same-origin scripts only, no inline script, no CDN.
2. **Login with any key.**
   - `GET /v1/me` returns `{name, scopes, role}` for any valid key: `owner` (admin), `public` (`public:read`) or
     `leads` (`leads:write` only).
   - The console shows the role and offers logout.
   - The key is kept in sessionStorage (localStorage only with "remember").
3. **Conversations.**
   - Soft delete through `deleted_at`: hidden from lists and details, and audited.
   - The owner lists owner conversations and all public (visitor) ones, each tagged by channel and mode. Public
     ones are read-only.
   - A public key lists, opens, renames and soft-deletes only conversations whose visitor hash matches its own.
4. **Turn trace.** Owner assistant turns store a trace:
   - the agents, the steps (agent, status, model, tier, action), the models, the tier and the sources;
   - the model's reasoning, in full up to 20,000 characters; for the super agent, per step.
   - Public turns never store reasoning.
   - The conversation detail returns the trace.
5. **Agents API.**
   - `GET /admin/v1/agents` lists each agent's effective model settings and their source.
   - `PUT /admin/v1/agents/{name}` overrides them live, stored in MongoDB.
   - `DELETE /admin/v1/agents/{name}` restores `config/agents.yaml`.
   - Validated like the YAML; a cloud model is never accepted as `confidential_model`. Audited.
6. **Console chat.**
   - **Sidebar:** history grouped by date (Today, Yesterday, Previous 7 days, Previous 30 days, then by month),
     tagged (`owner·super`, `owner·prep`, `owner·ask`, `owner·intake`, `public`), with search, open, rename,
     delete and new chat.
   - **Pickers:** an agent (Super, Ask, Prep, Intake, Public; a public key gets Public only) and a model (Auto or
     an inventory id).
   - **Live view:** plan, steps, agent, model and tier as status lines; reasoning in a collapsible panel; the
     answer as safely rendered markdown; sources; prep checks and removed details; intake drafts with Yes/No.
   - **Old conversations:** the stored trace, with the reasoning shown in its first lines and expanded on click.
   - **Quick-start prompts** per agent.
7. **Console tabs for the rest of the API:** Ingest, Review, Records, Profile, Memories, Models & agents, Leads,
   Security. Tabs are gated by role.
8. **Windows `make run`** starts the server in its own PowerShell window (through `scripts/dev.ps1 run`), so
   Ctrl+C shows the full shutdown and then a clean prompt. Other systems are unchanged.

## Non-goals
Multi-user accounts; editing visitor conversations; a public deployment of admin features (the public profile
still mounts no admin routes); tool-calling agents (no agent uses tools yet); a reasoning summary (later).

## Acceptance criteria
1. `/ui/` is served without a key, with the CSP and assets; `/` redirects to it. The API routes are not
   shadowed by the static mount.
2. `/v1/me` returns the right role for admin, public and leads keys, and 401 without a valid key.
3. The owner list includes public conversations, tagged. A public key sees only its own conversations, and
   another visitor's returns 404. A soft-deleted conversation is gone from lists and details, and the delete is
   audited.
4. A super-agent turn stores its steps with per-step reasoning; the reasoning is capped at 20,000 characters; a
   public turn has no reasoning. The detail returns the trace.
5. Pinning a model for prep makes the next prep answer use it; reset restores auto; a cloud model as
   `confidential_model` is refused with 422.
6. The console logs in with each key type, shows only the tabs the role allows, streams a super-agent answer
   with its plan and reasoning, renders a stored trace, and renames and deletes a conversation (live run in the
   browser).
7. The Windows `make run` command line is the new-window form; other systems are unchanged.
8. `make check` passes; a live run is reported in the PR.
