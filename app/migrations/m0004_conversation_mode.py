"""Mark existing conversations as `ask` mode; intake conversations are new from here on.

Only conversations without a mode are touched, so running it again changes nothing.
"""

from __future__ import annotations

from typing import Any

from pymongo.asynchronous.database import AsyncDatabase

DESCRIPTION = "set mode ask on existing conversations"


async def up(db: AsyncDatabase[dict[str, Any]]) -> None:
    await db["conversations"].update_many({"mode": {"$exists": False}}, {"$set": {"mode": "ask"}})
