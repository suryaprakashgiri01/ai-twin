"""Ship what the public deployment needs: `make publish` (Windows: ./scripts/dev.ps1 publish).

1. Mirrors the vault's kb_public collection (vectors and payloads) to the public Qdrant at
   PUBLISH_QDRANT_URL / PUBLISH_QDRANT_API_KEY, and deletes points there that are no longer
   published. kb_public only ever holds published, public-tier chunks; nothing else is read.
2. Writes the canary hash set (hashes only) to vault/publish/canary-hashes.json and the profile's
   resume to vault/publish/<resume file>, for the host's secret files
   (PUBLIC_CANARY_HASHES_PATH, PUBLIC_RESUME_PATH).

A dry run unless given --apply: it prints counts, never values.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from qdrant_client import AsyncQdrantClient, models

from app.core.config import Settings
from app.guardrails.canary_hashes import MongoCanaryHashes, sync_document
from app.kb.vectors import PUBLIC_COLLECTION
from app.vector.qdrant import create_qdrant, ensure_collections
from scripts._common import fail, info, ok, warn

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "vault" / "publish"
BATCH = 256


@dataclass(frozen=True)
class MirrorPlan:
    upsert: int
    delete: int


async def _ids(client: AsyncQdrantClient, collection: str) -> dict[str, models.ExtendedPointId]:
    """Every point id, keyed by its text (ids keep their own type for the delete call)."""
    ids: dict[str, models.ExtendedPointId] = {}
    offset = None
    while True:
        records, offset = await client.scroll(
            collection, limit=BATCH, offset=offset, with_payload=False, with_vectors=False
        )
        ids |= {str(r.id): r.id for r in records}
        if offset is None:
            return ids


async def mirror(
    source: AsyncQdrantClient,
    target: AsyncQdrantClient,
    *,
    apply: bool,
    collection: str = PUBLIC_COLLECTION,
) -> MirrorPlan:
    """Make the target's collection equal the source's. Returns what changed (or would)."""
    stale = await _ids(target, collection) if await target.collection_exists(collection) else {}
    seen: set[str] = set()
    offset = None
    upserted = 0
    while True:
        records, offset = await source.scroll(
            collection, limit=BATCH, offset=offset, with_payload=True, with_vectors=True
        )
        if records:
            seen |= {str(r.id) for r in records}
            upserted += len(records)
            if apply:
                points = [
                    models.PointStruct(id=r.id, vector=r.vector or {}, payload=r.payload or {})
                    for r in records
                ]
                await target.upsert(collection, points=points, wait=True)
        if offset is None:
            break
    gone = [pid for key, pid in stale.items() if key not in seen]
    if apply and gone:
        await target.delete(collection, points_selector=models.PointIdsList(points=gone), wait=True)
    return MirrorPlan(upsert=upserted, delete=len(gone))


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


async def export_canaries(path: Path, *, apply: bool) -> int:
    hashes = await MongoCanaryHashes(ttl_seconds=0).hashes()
    if apply:
        body = {**sync_document([]), "hashes": sorted(hashes)}
        await asyncio.to_thread(_write, path, json.dumps(body, indent=2).encode())
    return len(hashes)


async def run(apply: bool) -> int:
    from app.core.resources import open_resources
    from app.services.resume import resume_provider

    settings = Settings()
    if not settings.publish_qdrant_url:
        fail("Set PUBLISH_QDRANT_URL (and PUBLISH_QDRANT_API_KEY) in .env to the public Qdrant")
        return 1
    target = AsyncQdrantClient(
        url=settings.publish_qdrant_url,
        api_key=settings.publish_qdrant_api_key.get_secret_value() or None,
        timeout=30,
        check_compatibility=False,
    )
    async with open_resources(settings) as resources:
        source = create_qdrant(settings)
        try:
            if apply:
                await ensure_collections(target, "public", settings.embedding_dim)
            plan = await mirror(source, target, apply=apply)
        finally:
            await source.close()
            await target.close()
        verb = "mirrored" if apply else "would mirror"
        pruned = "deleted" if apply else "would delete"
        ok(f"kb_public: {verb} {plan.upsert} point(s), {pruned} {plan.delete} stale")
        count = await export_canaries(OUT / "canary-hashes.json", apply=apply)
        where = "written to vault/publish/canary-hashes.json" if apply else "dry run"
        info(f"canary hashes: {count} ({where})")
        resume = await resume_provider(settings, resources.profile, resources.blobs).open()
        if resume is None or resume.data is None:
            warn("no resume set in the profile; the public /v1/resume will 404")
        elif apply:
            await asyncio.to_thread(_write, OUT / resume.filename, resume.data)
            info(f"resume: vault/publish/{resume.filename} (PUBLIC_RESUME_PATH on the host)")
    if not apply:
        info("dry run: add --apply to publish")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Publish kb_public and the public files")
    parser.add_argument("--apply", action="store_true", help="really write (default: dry run)")
    return asyncio.run(run(parser.parse_args(argv).apply))


if __name__ == "__main__":
    sys.exit(main())
