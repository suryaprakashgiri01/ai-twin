"""Owner memories on the real MongoDB: the live-only unique norm index, bumps and archive."""

from __future__ import annotations

import pytest

from app.core.resources import Resources
from app.services.memory import MemoryIn, MemoryService, MongoMemoryStore
from tests.memory.fakes import FakeAudit

pytestmark = pytest.mark.integration


async def test_mongo_store_dedupes_live_items_only(resources: Resources) -> None:
    svc = MemoryService(MongoMemoryStore(), FakeAudit())
    first, created = await svc.add(MemoryIn(text="Use British spelling"), "key:test")
    again, created_again = await svc.add(MemoryIn(text="use british spelling!"), "key:test")
    assert created and not created_again and again.id == first.id and again.uses == 2
    await svc.archive(first.id, "key:test")
    fresh, created = await svc.add(MemoryIn(text="Use British spelling"), "key:test")
    assert created and fresh.id != first.id  # an archived item does not block a new one
    assert [m.id for m in await svc.listing("archived")] == [first.id]
    assert [m.id for m in await svc.active()] == [fresh.id]
