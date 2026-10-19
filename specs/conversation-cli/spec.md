# Spec: conversation-cli

Status: approved (requested directly by the owner) · Owner: Surya Prakash Giri · Ticket: none

## Problem
The owner chats with the vault over curl, and conversations exist only as 32-hex ids. To pick up an earlier
thread they need to find its id and send it in JSON by hand. They want a terminal command that lists their
conversations with a readable short name and a one-line description, and another that resumes one: by name
or id, with its earlier context, on macOS and Windows alike.

## Goals
1. Every owner conversation has a **name**: a short, unique, hyphenated slug such as
   `system-design-interview-prep`. It also has a **description**, one line of at most 100 characters. Both are
   set when the conversation's first question arrives:
   - the name comes from the first question's key words (stop words dropped, at most 4 words, at most 40
     characters), with a `-2`, `-3` … suffix when the name is taken
   - the description is the first question, whitespace-collapsed and truncated with `…`
   - no model call, so naming is instant, deterministic and works without Ollama
2. A migration backfills names and descriptions for existing owner conversations from their first user turn.
3. API (vault, `admin`):
   - `GET /admin/v1/conversations` includes `name` and `description`
   - `GET /admin/v1/conversations/by-name/{name}` returns the summary
   - `PATCH /admin/v1/conversations/{id}` with `{name?, description?}`. The name must match
     `^[a-z0-9]+(-[a-z0-9]+)*$` (at most 48 characters); a name already taken gives `409 CONFLICT`. Audited.
4. Terminal client `scripts/chat.py`. It calls the running API with `AI_TWIN_ADMIN_KEY` from `.env`; the key is
   never printed.
   - `list [--limit N]`: a table of name, id, last updated, turns and description.
   - `new`: an interactive chat.
   - `continue <name|id>`: prints the last few turns as a recap, then continues the chat. The server builds
     the model's context the usual way: Redis/Valkey while the TTL lasts (14 days for owner conversations),
     MongoDB after that, then the cache is refilled. The client sends only the conversation id.
   - `rename <name|id> <new-name> [--description TEXT]`.
   - Interactive loop: answers stream token by token. After each answer a dim line shows tier, model, latency
     and cited sources. `/exit` (or Ctrl+C / Ctrl+D) leaves cleanly.
   - Clear messages for: API not running, key missing or invalid, conversation not found, a name prefix
     matching more than one conversation.
5. Commands: `make chat` / `make chat-list` / `make chat-continue ARGS="<name|id>"`. On Windows:
   `./scripts/dev.ps1 chat`, `chat list`, `chat continue <name|id>`.

## Non-goals
The intake assistant (its own feature), deleting conversations, the public channel (visitors never get names),
model-generated titles.

## Acceptance criteria
1. A new owner conversation gets a slug name and a description from its first question. A second conversation
   with the same opening gets `-2`.
2. The backfill names existing owner conversations and leaves public ones untouched. Running it twice changes
   nothing.
3. `by-name` returns the conversation; an unknown name is `404`. A `PATCH` to a taken name is `409`; an invalid
   slug is `422`.
4. `continue` resumes by full name, unique name prefix or id. An ambiguous prefix lists the candidates and
   exits 1.
5. After the Redis entry is deleted (simulating TTL expiry), `continue` still answers with the earlier context,
   loaded from MongoDB, and the Redis entry exists again afterwards.
6. A missing `AI_TWIN_ADMIN_KEY`, the API being down, and a 401 each print one actionable line and exit 1, with no
   traceback.
7. No key value and no conversation text appears in API logs. The CLI prints conversation text only to the local
   terminal.
8. `make check` passes; the CLI's SSE parsing and name resolution are unit-tested with a fake transport.
