"""Name and describe existing owner conversations from their first user turn.

Public conversations are left alone: visitors never get names. Already-named conversations are
skipped, so running it again changes nothing.
"""

from __future__ import annotations

from typing import Any

from pymongo.asynchronous.database import AsyncDatabase

from app.services.naming import describe, fallback_name, slugify_title, unique_name

DESCRIPTION = "name and describe existing owner conversations"


async def up(db: AsyncDatabase[dict[str, Any]]) -> None:
    conversations = db["conversations"]
    taken = {
        doc["name"]
        async for doc in conversations.find(
            {"channel": "owner", "name": {"$type": "string"}}, {"name": 1}
        )
    }
    cursor = conversations.find({"channel": "owner", "name": {"$in": [None]}}, {"_id": 1}).sort(
        "created_at", 1
    )
    async for conversation in cursor:
        first = await db["messages"].find_one(
            {"conversation_id": conversation["_id"], "role": "user"}, sort=[("seq", 1)]
        )
        question = str(first.get("content", "")) if first else ""
        name = unique_name(slugify_title(question, fallback_name(conversation["_id"])), taken)
        taken.add(name)
        await conversations.update_one(
            {"_id": conversation["_id"]},
            {"$set": {"name": name, "description": describe(question) or None}},
        )
