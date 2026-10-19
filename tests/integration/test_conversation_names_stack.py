from __future__ import annotations

import pytest

from app.core.config import Settings
from app.core.resources import Resources
from app.migrations import m0003_conversation_names

pytestmark = pytest.mark.integration


async def test_backfill_names_owner_conversations_once(
    resources: Resources, settings: Settings
) -> None:
    db = resources.mongo[settings.mongodb_database]
    await db["conversations"].insert_many(
        [
            {"_id": "o1", "channel": "owner", "created_at": 1},
            {"_id": "o2", "channel": "owner", "created_at": 2},
            {"_id": "p1", "channel": "public", "created_at": 3},
        ]
    )
    question = "How many interviews did I have in September?"
    await db["messages"].insert_many(
        [
            {"conversation_id": cid, "seq": 1, "role": "user", "content": question}
            for cid in ("o1", "o2", "p1")
        ]
    )
    await m0003_conversation_names.up(db)
    first = await db["conversations"].find_one({"_id": "o1"})
    second = await db["conversations"].find_one({"_id": "o2"})
    public = await db["conversations"].find_one({"_id": "p1"})
    assert first and second and public
    assert first["name"] == "interviews-september"
    assert second["name"] == "interviews-september-2"
    assert first["description"] == question
    assert "name" not in public
    await m0003_conversation_names.up(db)
    again = await db["conversations"].find_one({"_id": "o1"})
    assert again and again["name"] == "interviews-september"
