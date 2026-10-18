"""observability-alerts T7 against the stack: events stored by MongoSecurityEvents go through the
Mongo event store and the alert worker into rendered emails (fake SMTP), with no details text."""

from __future__ import annotations

from datetime import timedelta
from zoneinfo import ZoneInfo

import pytest

from app.core.resources import Resources
from app.db.documents import SecurityEvent, utcnow
from app.security.alerts import MongoEventStore
from app.security.worker import AlertSettings, AlertWorker
from tests.leads.fakes import OWNER, FakeSmtp

pytestmark = pytest.mark.integration


async def test_events_alert_digest_and_list_through_mongo(resources: Resources) -> None:
    events = resources.security_events
    secret = "visitor wrote: I am Ravi, ravi@acme.example"
    await events.record(
        "canary_hit",
        "critical",
        rules=["canary_marker"],
        visitor_hash="b" * 64,
        details={"conversation_id": "c" * 32, "note": secret},
    )
    await events.record("auth_failed", "medium", rules=["AUTH_INVALID"], surface="admin")
    await events.record("meta_probe", "low", rules=["scope:model"])
    stored = await SecurityEvent.find_all().to_list()
    assert {e.kind for e in stored} == {"canary_hit", "auth_failed", "meta_probe"}
    assert next(e for e in stored if e.kind == "auth_failed").surface == "admin"

    store, smtp = MongoEventStore(), FakeSmtp()
    now = utcnow() + timedelta(seconds=1)
    worker = AlertWorker(
        store,
        smtp,
        resources.redis,
        AlertSettings(digest_hour=0, timezone=ZoneInfo("Asia/Kolkata")),
        clock=lambda: now,
    )
    await worker.tick(now)
    subjects = sorted(str(m["Subject"]) for _, m in smtp.sent)
    assert subjects[0] == "ai-twin alert · critical · canary_hit"
    assert subjects[1].startswith("ai-twin daily security digest · ")
    assert smtp.recipients() == {OWNER}
    for _, message in smtp.sent:
        rendered = message.as_string()
        assert "Ravi" not in rendered and "acme" not in rendered and "c" * 32 not in rendered

    after = {e.kind: e for e in await SecurityEvent.find_all().to_list()}
    assert after["canary_hit"].alerted_at is not None
    assert after["meta_probe"].alerted_at is None, "digest-only"
    assert all(e.digested_at is not None for e in after.values())
    assert await store.pending({"canary_hit"}, now - timedelta(hours=1)) == []

    newest = await store.recent(limit=10)
    assert newest[0].kind == "meta_probe"
    assert [e.kind for e in await store.recent(kind="auth_failed")] == ["auth_failed"]

    indexes = await SecurityEvent.get_pymongo_collection().index_information()
    assert {"alert_time", "digest_time", "time"} <= set(indexes)
