"""Build and refresh the knowledge base from a manifest: `python -m scripts.kb_sync [...]`.

  (no flags)          dry run: each entry as new, changed or unchanged; nothing reaches the
                      API (local_repo digests are refreshed under vault/kb/docs/generated/)
  --apply             ingest new entries; re-ingest changed ones (their old documents go)
  --only KEY[,KEY]    limit the run to these manifest keys
  cards [--apply]     write missing document cards (overview summaries) for indexed documents
  --reset --apply     export an encrypted backup first, then delete every source, document,
                      chunk, canary and vector; keys, profile, records, leads, memories and
                      conversations are kept. Run --apply again afterwards to load afresh.

The manifest and the files it names are the owner's data, so they live in the git-ignored
vault: `vault/kb/manifest.yaml` and `vault/kb/docs/`. What a run created is recorded in
`vault/kb/state.json` (entry key -> checksum, source and document ids), which makes a second
run a no-op. Each entry has a `key` and exactly one of `file`, `text`, `url` or `repos`:

    entries:
      - key: resume
        file: docs/resume.pdf          # relative to vault/kb/, or an absolute path
        category: resume
      - key: portfolio
        url: https://example.vercel.app/
        category: profile-link
      - key: work-notes
        file: docs/work-notes.html
        category: work-note
        tier: confidential             # a hint: it can only raise the tier
        release: local                 # if the employer guard quarantines it: local use only
      - key: repos
        repos: [owner/one, owner/two]  # must be in GITHUB_SYNC_REPOS
        tags: [github]
      - key: work-repo
        local_repo: ~/code/some-repo   # a local clone; nothing is fetched or sent
        author: me@work.example        # whose commits to summarise
        category: company-projects     # confidential: local models only
        release: local

A `local_repo` entry is turned into a Markdown digest (README, docs, languages, layout and the
author's own commits) under `vault/kb/docs/generated/` and ingested like a note.

The script issues a temporary admin key in MongoDB and revokes it when done, like `seed`.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import mimetypes
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import httpx
import yaml

from app.core.logging import setup_logging
from scripts import repo_digest
from scripts._common import ROOT, fail, info, ok, read_env, warn
from scripts.seed import admin_client

KB_DIR = ROOT / "vault" / "kb"
MANIFEST = KB_DIR / "manifest.yaml"
STATE = KB_DIR / "state.json"
TERMINAL = frozenset({"done", "quarantined", "failed"})
KINDS = ("file", "text", "url", "repos", "local_repo")
# Built from a local git repository by scripts.repo_digest; git-ignored like the rest of vault/.
GENERATED = "docs/generated"
META_KEYS = ("category", "tags", "tier", "summary", "occurredAt")


class SyncError(Exception):
    """A manifest or API problem the owner can fix; the message says how."""


@dataclass(frozen=True)
class Entry:
    key: str
    kind: str
    value: Any
    meta: dict[str, Any]
    title: str | None = None
    release: str | None = None
    # local_repo: whose commits the digest summarises (an e-mail or a name).
    author: str | None = None

    def path(self) -> Path:
        if self.kind == "local_repo":
            return KB_DIR / GENERATED / f"{self.key}.md"
        raw = Path(str(self.value)).expanduser()
        return raw if raw.is_absolute() else KB_DIR / raw

    def build(self) -> None:
        """Write a local_repo entry's digest; a new commit changes its checksum."""
        if self.kind != "local_repo":
            return
        try:
            text = repo_digest.digest(Path(str(self.value)), self.author or "", self.title)
        except repo_digest.DigestError as exc:
            raise SyncError(f"{self.key}: {exc}") from exc
        target = self.path()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    def checksum(self) -> str:
        digest = hashlib.sha256(json.dumps(self.meta, sort_keys=True).encode())
        self.build()
        if self.kind in ("file", "text", "local_repo"):
            path = self.path()
            if not path.is_file():
                raise SyncError(f"{self.key}: {path} does not exist")
            digest.update(path.read_bytes())
        else:
            digest.update(json.dumps(self.value, sort_keys=True).encode())
        return digest.hexdigest()


@dataclass
class EntryState:
    checksum: str
    sources: list[str] = field(default_factory=list)
    documents: list[str] = field(default_factory=list)


def parse_entries(data: Any) -> list[Entry]:
    if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
        raise SyncError(f"{MANIFEST} needs a top-level `entries:` list")
    entries: list[Entry] = []
    seen: set[str] = set()
    for raw in data["entries"]:
        key = str(raw.get("key") or "")
        kinds = [k for k in KINDS if k in raw]
        if not key or key in seen or len(kinds) != 1:
            raise SyncError(f"entry {key or raw!r}: needs a unique `key` and one of {KINDS}")
        if kinds[0] != "repos" and not raw.get("category"):
            raise SyncError(f"entry {key}: `category` is required")
        if kinds[0] == "local_repo" and not raw.get("author"):
            raise SyncError(f"entry {key}: `author` (your commit e-mail) is required")
        seen.add(key)
        meta = {k: raw[k] for k in META_KEYS if k in raw}
        entries.append(
            Entry(
                key,
                kinds[0],
                raw[kinds[0]],
                meta,
                raw.get("title"),
                raw.get("release"),
                raw.get("author"),
            )
        )
    return entries


def load_manifest(path: Path | None = None) -> list[Entry]:
    path = path or MANIFEST
    if not path.exists():
        raise SyncError(f"No manifest at {path}; see `python -m scripts.kb_sync --help`")
    return parse_entries(yaml.safe_load(path.read_text(encoding="utf-8")))


def load_state(path: Path | None = None) -> dict[str, EntryState]:
    path = path or STATE
    if not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    return {k: EntryState(**v) for k, v in raw.items()}


def save_state(state: dict[str, EntryState], path: Path | None = None) -> None:
    path = path or STATE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({k: asdict(v) for k, v in state.items()}, indent=2), "utf-8")


def plan(entries: list[Entry], state: dict[str, EntryState]) -> list[tuple[Entry, str]]:
    """(entry, "new" | "changed" | "unchanged") in manifest order."""
    out = []
    for entry in entries:
        known = state.get(entry.key)
        checksum = entry.checksum()  # also builds a local_repo digest, new or not
        status = "new" if known is None else "unchanged"
        if known is not None and known.checksum != checksum:
            status = "changed"
        out.append((entry, status))
    return out


# --- API --------------------------------------------------------------------------------------


def result(response: httpx.Response) -> Any:
    if response.status_code >= 400:
        try:
            message = response.json().get("message") or response.text[:200]
        except ValueError:
            message = response.text[:200]
        raise SyncError(f"{response.request.method} {response.request.url.path}: {message}")
    return response.json()["result"]


def submit(client: httpx.Client, entry: Entry, refresh: bool) -> list[str]:
    """Source ids the API queued (or already had) for the entry."""
    meta = dict(entry.meta)
    if entry.kind == "repos":
        body = {"repos": list(entry.value), "tags": meta.get("tags", []), "refresh": refresh}
        return [s["sourceId"] for s in result(client.post("/ingest/github", json=body))["sources"]]
    if entry.kind == "url":
        urls = entry.value if isinstance(entry.value, list) else [entry.value]
        body = {**meta, "urls": urls, "refresh": refresh}
        return [s["sourceId"] for s in result(client.post("/ingest/urls", json=body))["sources"]]
    path = entry.path()
    if entry.kind in ("text", "local_repo"):
        default = Path(str(entry.value)).name if entry.kind == "local_repo" else path.stem
        title = entry.title or default.replace("-", " ").title()
        body = {**meta, "title": title, "body": path.read_text(encoding="utf-8")}
        return [result(client.post("/ingest/text", json=body))["sourceId"]]
    content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    # The file form takes tags comma-separated.
    form = {k: (",".join(v) if isinstance(v, list) else str(v)) for k, v in meta.items()}
    files = {"files": (path.name, path.read_bytes(), content_type)}
    batch = result(client.post("/ingest/files", data=form, files=files))
    return [s["sourceId"] for s in batch["sources"]]


def wait_for(
    client: httpx.Client, source_id: str, timeout: float, interval: float = 1.0
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while True:
        status: dict[str, Any] = result(client.get(f"/sources/{source_id}"))
        if status["status"] in TERMINAL or time.monotonic() >= deadline:
            return status
        time.sleep(interval)


def release_quarantined(client: httpx.Client, entry: Entry, documents: list[str]) -> int:
    if entry.release != "local":
        return 0
    released = 0
    for document_id in documents:
        doc = result(client.get(f"/documents/{document_id}"))
        if doc["reviewStatus"] == "quarantined":
            client.post(f"/documents/{document_id}/release", json={"localOnly": True})
            released += 1
    return released


def sync_entry(
    client: httpx.Client, entry: Entry, old: EntryState | None, timeout: float
) -> EntryState:
    refresh = old is not None
    if old is not None and entry.kind in ("file", "text", "local_repo"):
        # New bytes make a new source; the previous documents are removed first.
        for document_id in old.documents:
            client.delete(f"/documents/{document_id}")
    sources = submit(client, entry, refresh)
    documents: list[str] = []
    failed = 0
    for source_id in sources:
        status = wait_for(client, source_id, timeout)
        documents += status.get("documentIds", [])
        if status["status"] not in ("done", "quarantined"):
            failed += 1
            reason = status.get("errorMessage") or status.get("errorCode") or ""
            warn(f"{entry.key}: source {source_id} ended {status['status']} {reason}".rstrip())
        elif status["status"] == "quarantined" and entry.release != "local":
            warn(f"{entry.key}: quarantined; add `release: local` to use it locally")
    released = release_quarantined(client, entry, documents)
    if released:
        info(f"{entry.key}: {released} quarantined document(s) released for local use")
    # A failed source leaves the entry unfinished: no checksum, so the next run tries again.
    return EntryState("" if failed else entry.checksum(), sources, documents)


def apply(
    client: httpx.Client,
    entries: list[Entry],
    state: dict[str, EntryState],
    *,
    timeout: float = 600,
) -> dict[str, int]:
    counts = {"new": 0, "changed": 0, "unchanged": 0}
    for entry, status in plan(entries, state):
        counts[status] += 1
        if status == "unchanged":
            continue
        info(f"{entry.key}: {status}, ingesting")
        state[entry.key] = sync_entry(client, entry, state.get(entry.key), timeout)
        save_state(state)  # a later failure keeps what is already done
        done = state[entry.key]
        (ok if done.checksum else warn)(
            f"{entry.key}: {len(done.documents)} document(s)"
            + ("" if done.checksum else "; not finished, the next --apply retries it")
        )
    return counts


def cards(client: httpx.Client) -> int:
    written = 0
    while True:
        batch = result(client.post("/documents/cards", json={"missingOnly": True, "limit": 20}))
        written += int(batch["written"])
        if batch["checked"] == 0 or batch["written"] == 0:
            return written


# --- reset ------------------------------------------------------------------------------------


async def _reset_stores() -> dict[str, int]:
    from qdrant_client import AsyncQdrantClient, models

    from app.core.config import Settings
    from app.db.client import init_mongo
    from app.db.documents import Canary, Chunk, KbDocument, Source
    from app.kb.vectors import ALL_COLLECTIONS

    settings = Settings()
    mongo = await init_mongo(settings, [Source, KbDocument, Chunk, Canary])
    counts: dict[str, int] = {}
    try:
        for name, model in (
            ("sources", Source),
            ("documents", KbDocument),
            ("chunks", Chunk),
            ("canaries", Canary),
        ):
            deleted = await model.get_pymongo_collection().delete_many({})
            counts[name] = deleted.deleted_count
    finally:
        await mongo.close()
    qdrant = AsyncQdrantClient(url=settings.qdrant_url, api_key=_secret(settings.qdrant_api_key))
    try:
        for collection in ALL_COLLECTIONS:
            if await qdrant.collection_exists(collection):
                await qdrant.delete(
                    collection_name=collection,
                    points_selector=models.FilterSelector(filter=models.Filter()),
                    wait=True,
                )
    finally:
        await qdrant.close()
    return counts


def _secret(value: Any) -> str | None:
    """An empty key means none, as in app.vector.qdrant (a local Qdrant has no key)."""
    return (value.get_secret_value() or None) if value is not None else None


def stores_down() -> list[str]:
    """MongoDB and Qdrant that do not answer, checked before anything asks for a passphrase."""
    from pymongo import MongoClient
    from pymongo.errors import PyMongoError

    from app.core.config import Settings

    settings = Settings()
    down: list[str] = []
    client: MongoClient[dict[str, Any]] = MongoClient(
        settings.mongodb_uri, serverSelectionTimeoutMS=2000
    )
    try:
        client.admin.command("ping")
    except PyMongoError:
        down.append(f"MongoDB at {settings.mongodb_uri}")
    finally:
        client.close()
    try:
        httpx.get(f"{settings.qdrant_url.rstrip('/')}/readyz", timeout=2).raise_for_status()
    except httpx.HTTPError:
        down.append(f"Qdrant at {settings.qdrant_url}")
    return down


def reset() -> int:
    from scripts import export

    down = stores_down()
    if down:
        fail(f"{' and '.join(down)} not reachable; nothing was backed up or deleted")
        info("Start the stack first: make up  (Windows: ./scripts/dev.ps1 up)")
        return 1
    info("Exporting an encrypted backup first (make export)")
    if export.main([]) != 0:
        fail("Backup failed; nothing was deleted")
        return 1
    counts = asyncio.run(_reset_stores())
    STATE.unlink(missing_ok=True)
    ok("Knowledge base cleared: " + ", ".join(f"{v} {k}" for k, v in counts.items()))
    info('Vectors removed from every collection. Load afresh: make kb-sync ARGS="--apply"')
    return 0


# --- CLI --------------------------------------------------------------------------------------


def parse(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("command", nargs="?", default="sync", choices=["sync", "cards"])
    parser.add_argument("--apply", action="store_true", help="actually call the API")
    parser.add_argument("--reset", action="store_true", help="back up, then clear the KB")
    parser.add_argument("--only", help="comma-separated manifest keys")
    parser.add_argument("--timeout", type=float, default=600, help="seconds per source")
    port = read_env().get("PORT") or "8000"
    parser.add_argument("--base-url", default=f"http://localhost:{port}")
    return parser.parse_args(argv)


def selected(entries: list[Entry], only: str | None) -> list[Entry]:
    if not only:
        return entries
    keys = {k.strip() for k in only.split(",") if k.strip()}
    unknown = keys - {e.key for e in entries}
    if unknown:
        raise SyncError(f"Not in the manifest: {', '.join(sorted(unknown))}")
    return [e for e in entries if e.key in keys]


def dry_run(args: argparse.Namespace) -> int:
    if args.reset:
        warn("Would export a backup, then delete every source, document, chunk and vector")
        info("Kept: API keys, profile, records, leads, memories, conversations. Add --apply")
        return 0
    if args.command == "cards":
        info("Would write missing document cards for indexed documents. Add --apply")
        return 0
    entries = selected(load_manifest(), args.only)
    for entry, status in plan(entries, load_state()):
        print(f"  {status:<9} {entry.key:<24} {entry.kind}: {entry.meta.get('category', '-')}")
    info("Add --apply to ingest the new and changed entries")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse(argv)
    setup_logging(level="WARNING", log_format="console")
    try:
        if not args.apply:
            return dry_run(args)
        if args.reset:
            return reset()
        with admin_client(args.base_url) as client:
            if args.command == "cards":
                ok(f"{cards(client)} card(s) written")
                return 0
            state = load_state()
            counts = apply(
                client, selected(load_manifest(), args.only), state, timeout=args.timeout
            )
        ok(", ".join(f"{v} {k}" for k, v in counts.items()))
        return 0
    except SyncError as exc:
        fail(str(exc))
        return 1
    except httpx.HTTPError as exc:
        fail(f"API call failed ({type(exc).__name__}); is the API running at {args.base_url}?")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
