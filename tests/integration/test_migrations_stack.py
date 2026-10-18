"""Migration rollback on the stack: down() undoes 0005 and 0004, an irreversible step blocks,
upgrade re-applies. Runs on the integration test database only."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import cast

import pytest

from app.core.resources import Resources
from app.db.documents import utcnow
from app.db.migrations import (
    IrreversibleMigrationError,
    applied_versions,
    rollback,
    rollback_plan,
    run_migrations,
)
from tests.integration.conftest import TEST_DATABASE

pytestmark = pytest.mark.integration


async def test_downgrade_then_upgrade_round_trip(resources: Resources) -> None:
    db = resources.mongo[TEST_DATABASE]
    await db["conversations"].insert_many(
        [
            {"_id": "c-ask", "channel": "owner", "mode": "ask"},
            {"_id": "c-intake", "channel": "owner", "mode": "intake"},
        ]
    )
    assert "0004" in await applied_versions()

    plan = await rollback_plan("0003")
    assert [m.version for m in plan] == ["0005", "0004"]
    assert await rollback(db, plan) == ["0005", "0004"]
    assert "0004" not in await applied_versions()
    modes = {d["_id"]: d.get("mode") async for d in db["conversations"].find({})}
    assert modes == {"c-ask": None, "c-intake": "intake"}

    assert await run_migrations(db) == ["0004", "0005"]
    modes = {d["_id"]: d.get("mode") async for d in db["conversations"].find({})}
    assert modes == {"c-ask": "ask", "c-intake": "intake"}


async def test_irreversible_step_blocks_before_any_change(resources: Resources) -> None:
    before = await applied_versions()
    with pytest.raises(IrreversibleMigrationError) as caught:
        await rollback_plan("0000")
    assert caught.value.versions == ["0003", "0002", "0001"]
    assert await applied_versions() == before


async def test_lead_defaults_round_trip(resources: Resources) -> None:
    db = resources.mongo[TEST_DATABASE]
    created = utcnow().replace(microsecond=0)
    await db["leads"].insert_one(
        {
            "_id": "old",
            "name": "Kavya",
            "email": "k@example.com",
            "message": "hi",
            "created_at": created,
        }
    )
    await db["leads"].insert_one(
        {"_id": "new", "name": "Ravi", "email": "r@example.com", "message": "hi", "status": "new"}
    )
    plan = await rollback_plan("0004")
    await rollback(db, plan)
    assert await run_migrations(db) == ["0005"]
    old = await db["leads"].find_one({"_id": "old"})
    assert old is not None
    assert (old["status"], old["source"], old["legacy"]) == ("reviewed", "form", True)
    expires = cast(datetime, old["expires_at"])
    assert expires - created == timedelta(days=365)
    await rollback(db, await rollback_plan("0004"))
    old = await db["leads"].find_one({"_id": "old"})
    new = await db["leads"].find_one({"_id": "new"})
    assert old is not None and new is not None
    assert "status" not in old and "legacy" not in old
    assert new["status"] == "new"
    await run_migrations(db)
