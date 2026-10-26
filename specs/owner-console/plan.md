# Plan: owner-console

## Architecture
```
browser /ui ── same-origin fetch (X-API-Key) ──▶ FastAPI
  login ─▶ GET /v1/me ─▶ role ─▶ tabs
  sidebar ─▶ GET /admin/v1/conversations?channel=… | GET /v1/conversations
  chat ─▶ POST /admin/v1/{super,chat,prep,assistant} | /v1/chat  (SSE: meta, step, thinking, token, sources, done)
  tabs ─▶ existing admin routes + /admin/v1/agents
server: StaticFiles /ui (after routers) · security headers on /ui · Message.trace · agent_model_overrides
```

## Key decisions
- **Static files, not a framework.** The pattern is proven in observable-ai-service, works under a strict CSP,
  needs no build step, and runs on both profiles.
- **SSE through fetch streams.** `EventSource` can't send the `X-API-Key` header.
- **Markdown built as DOM nodes.** No `innerHTML` anywhere, so answer text can never inject markup.
- **The trace lives on the message.** It's one document per turn, read with the detail, no joins. Reasoning is
  owner-only and capped.
- **Agent overrides layer over the YAML at request time.** A restart is never needed, and deleting the override
  restores the file's settings.
- **Visitor scoping uses the existing visitor hash.** A public key without `X-Visitor-Address` hashes to
  `key:{key_id}`, so its console chats are its own.

## Risks
- **UI scope is large.** Panels are independent modules that mount lazily; the chat panel comes first, the
  admin tabs after.
- **The 20,000-character cap** bounds the size of stored reasoning. Longer reasoning is cut, with a marker.
