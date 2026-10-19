"""Give existing conversations the summary fields' defaults; owner memories are new.

Only conversations without `summarized_turns` are touched, so running it again changes nothing.
The `owner_memories` indexes are declared on the document and created at startup. `down`
removes the defaults again from conversations that were never summarised; summaries and
memories written since are kept (they are owner data, not migration state).
"""

from __future__ import annotations

from typing import Any

from pymongo.asynchronous.database import AsyncDatabase

DESCRIPTION = "summary defaults on existing conversations"


async def up(db: AsyncDatabase[dict[str, Any]]) -> None:
    await db["conversations"].update_many(
        {"summarized_turns": {"$exists": False}}, {"$set": {"summarized_turns": 0}}
    )


async def down(db: AsyncDatabase[dict[str, Any]]) -> None:
    await db["conversations"].update_many(
        {"summarized_turns": 0, "summary": {"$in": [None]}}, {"$unset": {"summarized_turns": ""}}
    )
