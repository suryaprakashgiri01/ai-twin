"""Opportunities on the stack: the Mongo lead store (dedupe, due, list) and a pending lead draft
in MongoDB checkpoints that survives a Redis flush and a restart, then is pruned after 24 h
(AC5, AC9). Runs on the integration test database only."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from app.agents.checkpoint import MongoCheckpointSaver
from app.core.resources import Resources
from app.db.documents import LeadFields, utcnow
from app.leads.store import MongoLeadStore
from app.services.public_chat import prune_public, public_checkpointer
from tests.leads.harness import extraction, make_capture

pytestmark = pytest.mark.integration

VISITOR = "ravi.kumar@acme.example"


async def test_mongo_lead_store_round_trip(resources: Resources) -> None:
    store = MongoLeadStore()
    now = utcnow()
    lead = await store.insert(
        LeadFields(
            name="Ravi Kumar",
            email=VISITOR,
            company="Acme",
            role="Backend",
            message="Hi",
            email_key=VISITOR,
            company_key="acme",
            created_at=now,
        )
    )
    fetched = await store.get(lead.id)
    assert fetched is not None and fetched.id == lead.id and fetched.email == VISITOR
    assert await store.get("not-an-id") is None
    found = await store.recent_duplicate(VISITOR, "acme", now - timedelta(hours=24))
    assert found is not None and found.id == lead.id
    assert await store.recent_duplicate(VISITOR, "globex", now - timedelta(hours=24)) is None
    await store.merge_duplicate(lead.id, now)
    assert [d.id for d in await store.due(now, 10)] == [lead.id]
    await store.update(lead.id, {"next_attempt_at": now + timedelta(minutes=5)})
    assert await store.due(now, 10) == []
    updated = await store.update(lead.id, {"status": "suppressed", "triage": "spam"})
    assert updated is not None and updated.duplicates == 1
    assert [x.id for x in await store.list("suppressed", "spam", 5)] == [lead.id]
    assert [x.id for x in await store.undigested_suppressed(now + timedelta(seconds=1))] == [
        lead.id
    ]


async def _threads(saver: MongoCheckpointSaver) -> set[str]:
    return {str(t) for t in await saver.checkpoints.distinct("thread_id")}


async def test_a_pending_draft_survives_restart_and_is_pruned_after_a_day(
    resources: Resources,
) -> None:
    script: list[Any] = [
        extraction(role="backend", company="Acme", name="Ravi Kumar", message="Payment APIs"),
        extraction(email=VISITOR),
        "Ravi from Acme is hiring.",
    ]
    c = make_capture(script, saver=public_checkpointer())
    await c.say("I'm hiring for a backend role at Acme: payment APIs. I'm Ravi Kumar.")
    await c.say(VISITOR)
    assert await c.pending() and c.store.leads == {}

    # Redis flush and a restart: a new saver on the same collections still has the draft.
    await c.public.redis.flushall()
    restarted = public_checkpointer()
    config: Any = {"configurable": {"thread_id": f"public:{c.conversation_id}"}}
    snapshot = await c.public.service._graph.aget_state(config)
    tuple_ = await restarted.aget_tuple(config)
    assert tuple_ is not None and snapshot.interrupts
    assert snapshot.values["lead"]["draft"]["email"] == VISITOR

    # An intake thread is left alone; the public thread goes after 24 h, pending or not.
    intake = await restarted.checkpoints.find_one({})
    assert intake is not None
    await restarted.checkpoints.insert_one({**intake, "_id": "x", "thread_id": "intake-1"})
    assert await prune_public(restarted, utcnow()) == 0
    assert await prune_public(restarted, utcnow() + timedelta(hours=25)) == 1
    assert await _threads(restarted) == {"intake-1"}
