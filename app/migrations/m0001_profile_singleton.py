"""Create the profile singleton so readers never have to handle its absence."""

from __future__ import annotations

from typing import Any

from pymongo.asynchronous.database import AsyncDatabase

from app.db.documents import PROFILE_ID, Profile

DESCRIPTION = "create the profile singleton"


async def up(db: AsyncDatabase[dict[str, Any]]) -> None:
    default = Profile().model_dump(by_alias=True, exclude={"id"})
    await db["profile"].update_one({"_id": PROFILE_ID}, {"$setOnInsert": default}, upsert=True)
