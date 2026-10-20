"""observability-alerts T6: admin security routes (AC11): events without text, alert status,
an audited test alert."""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import httpx
import pytest
from fakeredis import FakeAsyncRedis

from app.auth.keys import issue_key
from app.core.app_factory import create_app
from app.core.config import Settings
from app.db.documents import Scope
from app.security.worker import AlertSettings, AlertWorker
from tests.conftest import FakeCheck
from tests.fakes import MemoryApiKeyStore, MemoryAuditLog
from tests.leads.fakes import FakeSmtp
from tests.security.fakes import T0, MemoryEventStore, minutes


class Api:
    def __init__(self, smtp: FakeSmtp | None) -> None:
        self.store = MemoryEventStore()
        self.smtp = smtp
        self.audit = MemoryAuditLog()
        self.keys = MemoryApiKeyStore()
        self.redis = FakeAsyncRedis()
        self.worker = AlertWorker(
            self.store,
            smtp,
            self.redis,
            AlertSettings(timezone=ZoneInfo("Asia/Kolkata")),
            clock=lambda: T0,
        )


async def client(api: Api, scopes: list[Scope] | None = None) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(
        Settings(_env_file=None, app_env="test", log_format="json"),
        health_checks=[FakeCheck("mongodb")],
    )
    app.state.resources = SimpleNamespace(
        api_keys=api.keys, audit=api.audit, redis=api.redis, security_events=None
    )
    app.state.alert_worker = api.worker
    key = (await issue_key(api.keys, "owner", scopes or ["admin"]))[1]
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", headers={"X-API-Key": key}
    ) as c:
        yield c


@pytest.fixture
def api() -> Api:
    return Api(FakeSmtp())


async def test_events_are_listed_newest_first_without_text(api: Api) -> None:
    api.store.add("meta_probe", T0, severity="low", rules=["scope:model"], visitor_hash="a" * 64)
    api.store.add("canary_hit", T0 + minutes(1), severity="critical", rules=["canary"])
    api.store.add("auth_failed", T0 + minutes(2), rules=["INVALID_API_KEY"], surface="admin")
    async for c in client(api):
        everything = (await c.get("/admin/v1/security/events")).json()
        critical = (await c.get("/admin/v1/security/events?severity=critical")).json()
        probes = (await c.get("/admin/v1/security/events?kind=meta_probe")).json()
        since = await c.get(
            "/admin/v1/security/events", params={"since": (T0 + minutes(1)).isoformat()}
        )
        bad = await c.get("/admin/v1/security/events?kind=not_a_kind")
    assert [e["kind"] for e in everything] == ["auth_failed", "canary_hit", "meta_probe"]
    assert set(everything[0]) == {
        "id", "kind", "severity", "rules", "visitor", "surface",
        "created_at", "alerted_at", "digested_at",
    }  # fmt: skip
    assert everything[0]["surface"] == "admin"
    assert probes[0]["visitor"] == "aaaaaaaa"
    assert [e["kind"] for e in critical] == ["canary_hit"]
    assert len(since.json()) == 2
    assert bad.status_code == 422


async def test_alert_status_shows_policies_and_cooldowns(api: Api) -> None:
    api.store.add("canary_hit", T0, severity="critical")
    await api.worker.alert_due(T0)
    async for c in client(api):
        status = (await c.get("/admin/v1/alerts/status")).json()
    assert status["smtp_configured"] is True
    assert status["cooldown_minutes"] == 30 and status["digest_hour"] == 9
    assert status["timezone"] == "Asia/Kolkata"
    policies = {p["name"]: p for p in status["policies"]}
    assert policies["canary_hit"]["cooldown_remaining_seconds"] > 0
    assert policies["canary_hit"]["last_sent"] is not None
    assert policies["meta_probe"]["mode"] == "digest"


async def test_test_alert_is_sent_and_audited(api: Api) -> None:
    async for c in client(api):
        response = await c.post("/admin/v1/alerts/test")
    assert response.json() == {"sent": True}
    assert api.smtp is not None
    [(_recipients, message)] = api.smtp.sent
    assert message["Subject"] == "ai-twin alert · low · test"
    [entry] = api.audit.entries
    assert entry["action"] == "alerts.test" and entry["actor"] == "key:owner"
    assert entry["sent"] is True and entry["target"] == "alerts"


async def test_test_alert_without_smtp_is_503() -> None:
    api = Api(None)
    async for c in client(api):
        response = await c.post("/admin/v1/alerts/test")
        status = (await c.get("/admin/v1/alerts/status")).json()
    assert response.status_code == 503
    assert status["smtp_configured"] is False
    assert api.audit.entries == []


async def test_routes_need_the_admin_scope(api: Api) -> None:
    async for c in client(api, scopes=["public:read"]):
        assert (await c.get("/admin/v1/security/events")).status_code == 403
        assert (await c.post("/admin/v1/alerts/test")).status_code == 403
