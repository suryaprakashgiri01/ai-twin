"""Mark existing conversations as `ask` mode; intake conversations are new from here on.

Only conversations without a mode are touched, so running it again changes nothing. `down`
removes the `ask` marks again; intake conversations keep their mode (the app wrote it, not this
migration).
"""

from __future__ import annotations

from typing import Any

from pymongo.asynchronous.database import AsyncDatabase

DESCRIPTION = "set mode ask on existing conversations"


async def up(db: AsyncDatabase[dict[str, Any]]) -> None:
    await db["conversations"].update_many({"mode": {"$exists": False}}, {"$set": {"mode": "ask"}})


async def down(db: AsyncDatabase[dict[str, Any]]) -> None:
    await db["conversations"].update_many({"mode": "ask"}, {"$unset": {"mode": ""}})
