# ai-twin

A personal knowledge base and AI twin. One FastAPI + LangGraph service with two faces:

- **Owner mode** (local vault): ingest notes, files, links and repos; ask anything across every data tier.
- **Public mode** (portfolio assistant): answers only from approved public facts, serves the resume and
  forwards opportunities by email.

Private data never leaves the local vault; the public deployment only ever holds published public chunks.

Status: bootstrapping. See `docs/hld.md` once it lands.
