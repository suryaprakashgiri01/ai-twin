"""Give every row the API names a `uid` (UUID v4), and the references to them their uid too.

Only rows without a `uid` (and references still unset) are touched, so running it again changes
nothing. Writes go in batches, so a large `security_events` collection is not one huge request.
`down` removes the fields again; the ObjectIds were never changed.
"""

from __future__ import annotations

import uuid
from typing import Any

from pymongo import UpdateOne
from pymongo.asynchronous.database import AsyncDatabase

DESCRIPTION = "public UUID v4 ids beside the ObjectIds"
COLLECTIONS = ("sources", "documents", "records", "leads", "owner_memories", "security_events")
BATCH = 500
UNSET: dict[str, Any] = {"$in": [None]}  # null or missing


async def _backfill(db: AsyncDatabase[dict[str, Any]], name: str) -> None:
    batch: list[UpdateOne] = []
    async for row in db[name].find({"uid": UNSET}, {"_id": 1}):
        batch.append(UpdateOne({"_id": row["_id"]}, {"$set": {"uid": str(uuid.uuid4())}}))
        if len(batch) == BATCH:
            await db[name].bulk_write(batch, ordered=False)
            batch = []
    if batch:
        await db[name].bulk_write(batch, ordered=False)


async def _source_uids(db: AsyncDatabase[dict[str, Any]]) -> None:
    async for source in db["sources"].find({}, {"_id": 1, "uid": 1}):
        await db["documents"].update_many(
            {"source_id": source["_id"], "source_uid": UNSET},
            {"$set": {"source_uid": source["uid"]}},
        )


async def _resume_uid(db: AsyncDatabase[dict[str, Any]]) -> None:
    profile = await db["profile"].find_one(
        {"resume_document_id": {"$ne": None}, "resume_document_uid": UNSET}
    )
    if profile is None:
        return
    doc = await db["documents"].find_one({"_id": profile["resume_document_id"]}, {"uid": 1})
    if doc is not None:
        await db["profile"].update_one(
            {"_id": profile["_id"]}, {"$set": {"resume_document_uid": doc["uid"]}}
        )


async def up(db: AsyncDatabase[dict[str, Any]]) -> None:
    for name in COLLECTIONS:
        await _backfill(db, name)
    await _source_uids(db)
    await _resume_uid(db)


async def down(db: AsyncDatabase[dict[str, Any]]) -> None:
    for name in COLLECTIONS:
        await db[name].update_many({"uid": {"$exists": True}}, {"$unset": {"uid": ""}})
    await db["documents"].update_many({}, {"$unset": {"source_uid": ""}})
    await db["profile"].update_many({}, {"$unset": {"resume_document_uid": ""}})
