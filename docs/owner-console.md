# Owner console

A browser console at `/ui` for every agent and API: log in with any API key, chat with the super agent and
each agent (live plan, steps, thinking and sources), browse past conversations, pin models per agent, and use
the ingest, review, records, profile, memories, models, leads and security APIs in tabs.

Code:
- `app/ui/static/`: `index.html`, `app.css`, `js/` (`api.js`, `auth.js`, `dom.js`, `markdown.js`, `sidebar.js`,
  `theme*.js`, `main.js`) and `js/panels/` (one module per tab)
- `app/middleware/security_headers.py`: CSP and headers for `/ui`
- `app/api/routes/console.py` (`/` → `/ui/`)
- `app/api/routes/public.py`: `/v1/me` and the visitor's `/v1/conversations`
- `app/services/owner_chat.py`: `ConversationDirectory`, which scopes conversations to who asks
- `app/services/conversations.py`: `TurnTrace`
- `app/services/agent_settings.py` and `app/api/routes/admin_agents.py`
- `app/services/model_status.py`
- `scripts/run.py`

## Who sees what

| Key | Role (`/v1/me`) | Console |
|---|---|---|
| `admin` | `owner` | Every agent and tab. History holds owner conversations plus every visitor conversation, tagged `public` and read-only. |
| `public:read` | `public` | The public assistant only. History holds only the conversations this key started. |
| `leads:write` | `leads` | The opportunity form only. |

The console is static files: every data call carries the key, and the API enforces the scope. On the public
deployment the admin routes are not mounted at all, so a deployed console can only chat publicly.

## Security

- **CSP:** `default-src 'self'; script-src 'self'`, with no inline script and no CDN.
- **Safe rendering:**
  - Text is always set with `textContent`. Markdown is built as DOM nodes; links are http, https or mailto only,
    with `rel=noopener`.
  - `tests/test_console.py` fails on any `innerHTML`, `eval` or inline script, and on an import that does not
    resolve.
- **The key:**
  - It is stored in sessionStorage, or in localStorage when "remember" is ticked. It is sent as `X-API-Key` to
    this origin only.
  - SSE streams are read with `fetch` and a `ReadableStream`, because `EventSource` cannot send headers.

## Conversations

| Route | Who | Does |
|---|---|---|
| `GET /admin/v1/conversations?channel=owner\|public\|all` | owner | Lists conversations; each summary has `channel` and `mode` |
| `GET /admin/v1/conversations/{id}` | owner | Returns any conversation, with turns and their trace |
| `PATCH` / `DELETE /admin/v1/conversations/{id}` | owner | Renames or soft-deletes owner conversations; 409 for a visitor's |
| `GET /v1/conversations`, `GET` / `PATCH` / `DELETE /v1/conversations/{id}` | public key | Only conversations whose `visitor_hash` is the caller's; anything else is 404 |

Soft delete sets `deleted_at`. The conversation leaves every list and read, it cannot be continued (its Redis
copy is dropped), and its turns stay stored. Deletes are audited.

## Turn trace

Owner answers (ask, prep, super) store `Message.trace`:
- the agents and steps (agent, status, model, tier, action);
- the models, the tier and the sources (index, document id, title);
- the model's reasoning.

For the super agent, each step's reasoning is stored with that step.

Reasoning is capped at 20,000 characters per turn, shared across steps in order. The trace is kept in MongoDB
only: the Redis history, and the reload that feeds prompts, leave it out. Public turns never carry a trace.

In the console, a live turn streams the plan, steps and thinking. An old turn replays its stored trace: the
reasoning shows its first lines and expands on click.

```mermaid
flowchart LR
    turn["Owner turn"] --> graph["owner / prep / super"]
    graph -->|"SSE: meta, step, thinking, token, sources, done"| live["Console, live"]
    graph --> store["append_turn"]
    store -->|"turn without trace"| redis["Redis history → prompts"]
    store -->|"turn with trace"| mongo["MongoDB messages"]
    mongo -->|"GET conversation"| replay["Console, stored trace"]
```

## Agent model settings

`config/agents.yaml` is the base. From the console, the Models tab, or with
`PUT /admin/v1/agents/{owner|prep|super|intake}`, you can override one agent's `model`, `prefer`,
`confidentialModel`, `confidentialPrefer` and `need`.

- The override is stored in `agent_model_overrides`.
- It is validated exactly like the file: an unknown id, or a cloud model as `confidentialModel`, is refused.
- It applies to the next call, because agents read their choice each step.
- `DELETE` resets the agent to the file. Both PUT and DELETE are audited.
- At startup, stored overrides are loaded; one that no longer fits the inventory is skipped with a warning.

## Model status

`GET /admin/v1/models` lists the inventory with the tier caps (overrides applied) and a `status` and `hint` per
model:

| Status | Meaning | Hint |
|---|---|---|
| `active` | It can answer here | — |
| `disabled` | Disabled in config or by an override | — |
| `no key` | The provider key is not set | `set GROQ_API_KEY in .env` |
| `not pulled` | Ollama does not have it (`/api/tags`) | `make ollama ARGS="pull <model>"` |
| `unreachable` | Ollama is down, or (with `verify`) the provider did not answer | — |
| `missing` | `verify=true` only: the provider no longer serves the id | update `config/models.yaml` |

`verify=true`, the Models tab's "Check providers", makes the same check as `make models-check`, which uses the
same endpoint table. The chat model picker offers only active models.

## Quick starts

An empty chat shows quick starts for the agent. Clicking one fills the message box, so you can edit it before
sending. The star in the message box saves its text as one of your own quick starts for that agent
(`POST /admin/v1/starters`, stored in `chat_starters`, at most 50 per agent). The × on a saved one deletes it.
Saves and deletes are audited.

## Lists and sources

- **Lists** (Review, Records, Memories, Leads, Security):
  - search, filters with readable labels and defaults (Review opens on pending), sortable headers;
  - 10, 20, 50 or 100 rows per page; first, previous, page, next and last;
  - a refresh icon that spins while loading.
  - In list-and-detail tabs, the first row is selected and highlighted.
- **Sources** under an answer are grouped by kind: GitHub repositories, work projects, profile and resume,
  notes, and other documents. Traces store each source's category and tier for this. Titles redacted to the
  same text (for example `[PERSON]`) show their category and a short document id. The redaction itself stays.

## `make run` on Windows

On Windows, `make` ends at Ctrl+C before the server has finished stopping. `scripts/run.py` therefore opens the
server in its own PowerShell window (`scripts/dev.ps1 run`, without make's variables). Ctrl+C there shows the
whole shutdown, then a clean prompt. `ARGS="--here"` keeps it in the current terminal. On macOS and Linux the
server runs in place, as before.
