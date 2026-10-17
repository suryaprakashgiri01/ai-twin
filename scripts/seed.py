"""Create or remove the fake-persona demo data: `python -m scripts.seed [--apply] [--remove]`.

  (no flags)            dry run: lists what would be created
  --apply               creates it through the running API (`make run`)
  --remove --apply      deletes exactly what a previous seed created
  --with-profile        also replaces the owner profile (never undone by --remove)

Fake persona only (Kavya Testpal). The script issues a temporary admin key in MongoDB, revokes it
when done, and records what it created in `vault/seed.json` so removal never touches real data.
Seeded records also carry a marker in their notes, so they can be found without the manifest.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from typing import Any

import httpx

from app.auth.keys import MongoApiKeyStore, issue_key
from app.core.config import Settings
from app.core.logging import setup_logging
from app.db.client import init_mongo
from app.db.documents import ApiKey, utcnow
from scripts._common import ROOT, fail, info, ok, read_env, warn

MARKER = "seed:fake-persona"
KEY_NAME = "seed-script"
MANIFEST = ROOT / "vault" / "seed.json"
TERMINAL = frozenset({"done", "quarantined", "failed"})

NOTES: tuple[dict[str, Any], ...] = (
    {
        "title": "Quorvex scheduler retrospective",
        "category": "project",
        "body": (
            "Kavya Testpal built the Quorvex scheduler at Initech Cloud. It used Raft for leader "
            "election, and the main lesson was to keep the state machine small and test the "
            "election timeouts under packet loss."
        ),
    },
    {
        "title": "Globex Labs offer",
        "category": "compensation",
        "tier": "confidential",
        "body": (
            "Globex Labs offered Kavya Testpal a Senior Backend Engineer role with a joining "
            "bonus and a four week notice requirement. Kavya is weighing it against the Initech "
            "Cloud counter-offer."
        ),
    },
)

RECORDS: tuple[dict[str, str], ...] = (
    {"kind": "application", "company": "Globex Labs", "stage": "applied", "date": "2026-09-02"},
    {"kind": "application", "company": "Umbrella Data", "stage": "applied", "date": "2026-09-10"},
    {"kind": "interview", "company": "Globex Labs", "stage": "onsite", "date": "2026-09-18"},
    {"kind": "interview", "company": "Initech Cloud", "stage": "screen", "date": "2026-09-29"},
    {"kind": "interview", "company": "Acme Widgets", "stage": "screen", "date": "2026-08-21"},
)

PROFILE: dict[str, Any] = {
    "availability": "open to offers",
    "notice_period": "4 weeks",
    "preferred_roles": ["Backend Engineer", "Platform Engineer"],
    "locations": ["Remote", "Pune"],
}


@dataclass
class Manifest:
    sources: list[str] = field(default_factory=list)
    documents: list[str] = field(default_factory=list)
    records: list[str] = field(default_factory=list)

    @classmethod
    def load(cls) -> Manifest | None:
        if not MANIFEST.exists():
            return None
        return cls(**json.loads(MANIFEST.read_text(encoding="utf-8")))

    def save(self) -> None:
        MANIFEST.parent.mkdir(parents=True, exist_ok=True)
        MANIFEST.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")


# --- API calls ------------------------------------------------------------------------------


def note_body(note: dict[str, Any], stamp: str) -> dict[str, Any]:
    # The stamp changes the checksum, so a seed after a removal is ingested again.
    body = f"{note['body']}\n\n({MARKER} {stamp})"
    return {**note, "body": body, "tags": [MARKER]}


def record_body(record: dict[str, str]) -> dict[str, str]:
    return {
        **record,
        "role": "Backend Engineer",
        "date": f"{record['date']}T10:00:00",
        "notes": MARKER,
    }


def wait_for(
    client: httpx.Client, source_id: str, timeout: float, interval: float
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while True:
        status: dict[str, Any] = client.get(f"/sources/{source_id}").raise_for_status().json()
        if status["status"] in TERMINAL or time.monotonic() >= deadline:
            return status
        time.sleep(interval)


def seed(
    client: httpx.Client, *, with_profile: bool, timeout: float = 120, interval: float = 1.0
) -> Manifest:
    manifest = Manifest()
    stamp = utcnow().isoformat(timespec="seconds")
    try:
        if with_profile:
            client.put("/profile", json=PROFILE).raise_for_status()
        for note in NOTES:
            submitted = client.post("/ingest/text", json=note_body(note, stamp)).raise_for_status()
            manifest.sources.append(submitted.json()["source_id"])
        for source_id in manifest.sources:
            status = wait_for(client, source_id, timeout, interval)
            manifest.documents.extend(status["document_ids"])
            if status["status"] != "done":
                warn(f"source {source_id} ended {status['status']}")
        for record in RECORDS:
            created = client.post("/records", json=record_body(record)).raise_for_status()
            manifest.records.append(created.json()["id"])
    finally:
        # Even a partial run is recorded, so --remove can undo it.
        manifest.save()
    return manifest


def remove(client: httpx.Client, manifest: Manifest | None) -> tuple[int, int]:
    documents = list(manifest.documents) if manifest else []
    records = set(manifest.records) if manifest else set()
    listed = client.get("/records").raise_for_status().json()
    records |= {r["id"] for r in listed if r.get("notes") == MARKER}
    for document_id in documents:
        client.delete(f"/documents/{document_id}")  # already gone is fine
    for record_id in sorted(records):
        client.delete(f"/records/{record_id}")
    MANIFEST.unlink(missing_ok=True)
    return len(documents), len(records)


# --- temporary key --------------------------------------------------------------------------


async def _issue() -> str:
    client = await init_mongo(Settings(), [ApiKey])
    try:
        return (await issue_key(MongoApiKeyStore(), KEY_NAME, ["admin"]))[1]
    finally:
        await client.close()


async def _revoke() -> None:
    client = await init_mongo(Settings(), [ApiKey])
    try:
        await MongoApiKeyStore().revoke(KEY_NAME, utcnow())
    finally:
        await client.close()


@contextmanager
def admin_client(base_url: str) -> Iterator[httpx.Client]:
    key = asyncio.run(_issue())
    try:
        with httpx.Client(
            base_url=f"{base_url}/admin/v1", headers={"X-API-Key": key}, timeout=30
        ) as client:
            yield client
    finally:
        asyncio.run(_revoke())


# --- CLI ------------------------------------------------------------------------------------


def describe(args: argparse.Namespace) -> None:
    if args.remove:
        manifest = Manifest.load()
        count = len(manifest.documents) if manifest else 0
        info(f"Would delete {count} seeded document(s) and every record marked '{MARKER}'")
        return
    info(f"Would ingest {len(NOTES)} notes: " + ", ".join(n["title"] for n in NOTES))
    info(f"Would add {len(RECORDS)} records (notes = '{MARKER}')")
    if args.with_profile:
        warn("Would replace the owner profile")
    info("Add --apply to do it")


def parse(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--apply", action="store_true", help="actually call the API")
    parser.add_argument("--remove", action="store_true", help="delete what a seed created")
    parser.add_argument("--with-profile", action="store_true", help="also replace the profile")
    port = read_env().get("PORT") or "8000"
    parser.add_argument("--base-url", default=f"http://localhost:{port}")
    return parser.parse_args(argv)


def execute(args: argparse.Namespace, client: httpx.Client) -> int:
    if args.remove:
        documents, records = remove(client, Manifest.load())
        ok(f"Removed {documents} document(s) and {records} record(s)")
        return 0
    if Manifest.load() is not None:
        fail("Already seeded (vault/seed.json exists). Remove first: --remove --apply")
        return 1
    manifest = seed(client, with_profile=args.with_profile)
    ok(f"Seeded {len(manifest.documents)} document(s) and {len(manifest.records)} record(s)")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse(argv)
    setup_logging(level="WARNING", log_format="console")
    if not args.apply:
        describe(args)
        return 0
    try:
        with admin_client(args.base_url) as client:
            return execute(args, client)
    except httpx.HTTPError as exc:
        fail(f"API call failed ({type(exc).__name__}); is the API running at {args.base_url}?")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
