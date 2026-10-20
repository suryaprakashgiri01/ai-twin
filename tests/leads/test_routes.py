"""Opportunities T5: the form endpoint and its scope, admin list/patch (audited) and the intake
`show_leads` action (AC4, AC5, AC6, AC10, AC11)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
from fakeredis import FakeAsyncRedis
from fastapi import FastAPI
from pydantic import SecretStr

from app.agents.intake.actions import ACTIONS, RunContext, catalog_text
from app.auth.keys import issue_key
from app.core.app_factory import create_app
from app.core.config import Settings
from app.db.documents import Scope
from app.leads.outbox import LeadOutbox
from app.leads.service import LeadLimits, LeadService
from app.leads.turnstile import Turnstile
from app.services.admin_ops import AdminOps
from tests.conftest import FakeCheck
from tests.envelope import items, result
from tests.fakes import MemoryApiKeyStore, MemoryAuditLog, MemoryOverrideStore
from tests.leads.fakes import (
    OWNER,
    FakeSmtp,
    MemoryLeadStore,
    StaticFacts,
    TriageClassifier,
    siteverify,
)
from tests.public.fakes import MemorySecurityEvents

VISITOR = "ravi.kumar@acme.example"
FORM = {
    "name": "Ravi Kumar",
    "email": VISITOR,
    "company": "Acme",
    "role": "Backend Engineer",
    "message": "Payment APIs in Python, remote.",
    "employment_type": "contract",
    "link": "https://jobs.acme.example/42",
}


class Stack:
    def __init__(self, *, secret: str = "", limits: LeadLimits | None = None) -> None:
        settings = Settings(
            _env_file=None,
            app_env="test",
            log_format="json",
            visitor_hash_salt=SecretStr("test-salt"),
            public_owner_name="Kavya",
        )
        self.app: FastAPI = create_app(settings, health_checks=[FakeCheck("mongodb")])
        self.keys = MemoryApiKeyStore()
        self.audit = MemoryAuditLog()
        self.events = MemorySecurityEvents()
        self.store = MemoryLeadStore()
        self.smtp = FakeSmtp()
        redis = FakeAsyncRedis(decode_responses=True)
        self.outbox = LeadOutbox(self.store, self.smtp, self.events, redis)
        self.app.state.resources = SimpleNamespace(
            api_keys=self.keys,
            audit=self.audit,
            overrides=MemoryOverrideStore(),
            redis=redis,
            security_events=self.events,
            leads=self.store,
            kb=None,
            vectors=None,
            blobs=None,
            profile=None,
            records=None,
        )
        self.app.state.leads = LeadService(
            self.store,
            self.outbox,
            redis,
            Turnstile(secret, transport=siteverify()),
            TriageClassifier(),
            StaticFacts(),
            self.events,
            salt="salt",
            limits=limits,
        )

    async def client(self) -> AsyncIterator[httpx.AsyncClient]:
        transport = httpx.ASGITransport(app=self.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client

    async def key(self, *scopes: Scope) -> str:
        return (await issue_key(self.keys, "-".join(scopes), list(scopes)))[1]


async def post(
    client: httpx.AsyncClient,
    key: str,
    body: dict[str, Any] | None = None,
    address: str = "203.0.113.9",
) -> httpx.Response:
    return await client.post(
        "/v1/opportunities",
        json=body or FORM,
        headers={"X-API-Key": key, "X-Visitor-Address": address},
    )


async def test_form_endpoint_runs_the_same_pipeline() -> None:
    s = Stack()
    key = await s.key("leads:write")
    async for client in s.client():
        response = await post(client, key)
        assert response.status_code == 202
        assert response.json() == {
            "status": "success",
            "message": "Thanks, your message was passed to Kavya.",
            "result": {
                "status": "received",
                "message": "Thanks, your message was passed to Kavya.",
            },
        }
        duplicate = await post(client, key, address="198.51.100.4")
        assert duplicate.status_code == 202
    [lead] = s.store.leads.values()
    assert (lead.source, lead.status, lead.duplicates) == ("form", "new", 1)
    assert lead.details.employment_type == "contract" and lead.details.link == FORM["link"]
    await s.outbox.tick()
    assert len(s.smtp.sent) == 1 and s.smtp.recipients() == {OWNER}


async def test_public_read_alone_is_forbidden() -> None:
    s = Stack()
    reader = await s.key("public:read")
    async for client in s.client():
        response = await post(client, reader)
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "FORBIDDEN"
        missing = await client.post("/v1/opportunities", json=FORM)
        assert missing.status_code == 401
    assert s.store.leads == {}
    assert "auth_failed" in s.events.kinds()


async def test_form_validation_is_done_in_code() -> None:
    s = Stack()
    key = await s.key("leads:write")
    async for client in s.client():
        bad = await post(client, key, {**FORM, "email": "nope", "cc": VISITOR})
        assert bad.status_code == 422
        crlf = await post(client, key, {**FORM, "company": "Acme\r\nBcc: x@evil.example"})
        assert crlf.status_code == 202
    [lead] = s.store.leads.values()
    assert lead.company == "Acme Bcc: x@evil.example"


async def test_form_turnstile_and_caps() -> None:
    s = Stack(secret="ts-secret", limits=LeadLimits(per_visitor_day=1))
    key = await s.key("leads:write")
    async for client in s.client():
        unverified = await post(client, key)
        assert unverified.status_code == 403
        verified = await post(client, key, {**FORM, "turnstile_token": "good-token"})
        assert verified.status_code == 202
        capped = await post(
            client, key, {**FORM, "company": "Globex", "turnstile_token": "good-token"}
        )
        assert capped.status_code == 429
        assert capped.headers["Retry-After"] == "86400"
        assert capped.json()["error"]["code"] == "RATE_LIMITED"
    assert len(s.store.leads) == 1
    rules = [e["rules"][0] for e in s.events.events if e["kind"] == "lead_refused"]
    assert rules == ["turnstile:missing", "lead_cap:visitor"]


async def test_admin_lists_and_patches_leads_with_an_audit_entry() -> None:
    s = Stack()
    form, admin = await s.key("leads:write"), await s.key("admin")
    async for client in s.client():
        await post(client, form)
        await post(client, form, {**FORM, "company": "Globex"}, address="198.51.100.4")
        listed = await client.get("/admin/v1/leads", headers={"X-API-Key": admin})
        assert listed.status_code == 200
        rows = items(listed)
        assert [r["company"] for r in rows] == ["Globex", "Acme"]
        assert rows[0]["link"] == FORM["link"] and rows[0]["triage"] == "recruiter"
        lead_id = rows[1]["id"]
        filtered = await client.get(
            "/admin/v1/leads",
            params={"filter": "status:new,triage:spam"},
            headers={"X-API-Key": admin},
        )
        assert items(filtered) == []
        patched = await client.patch(
            f"/admin/v1/leads/{lead_id}", json={"status": "replied"}, headers={"X-API-Key": admin}
        )
        assert patched.status_code == 200 and result(patched)["status"] == "replied"
        bad = await client.patch(
            f"/admin/v1/leads/{lead_id}",
            json={"status": "suppressed"},
            headers={"X-API-Key": admin},
        )
        assert bad.status_code == 422
        missing = await client.patch(
            "/admin/v1/leads/64b000000000000000000000",
            json={"status": "reviewed"},
            headers={"X-API-Key": admin},
        )
        assert missing.status_code == 404
        forbidden = await client.get("/admin/v1/leads", headers={"X-API-Key": form})
        assert forbidden.status_code == 403
    [entry] = s.audit.entries
    assert entry == {
        "actor": "key:admin",
        "action": "lead.update",
        "target": lead_id,
        "status": "replied",
        "from": "new",
    }


async def test_intake_show_leads_is_read_only(tmp_path: Path) -> None:
    s = Stack()
    key = await s.key("leads:write")
    async for client in s.client():
        await post(client, key)
    spec = ACTIONS["show_leads"]
    assert not spec.write and "show_leads" in catalog_text()
    ops = AdminOps.from_state(s.app.state)
    ctx = RunContext(ops=ops, actor="agent:intake via key:test", inbox=tmp_path, wait_seconds=0)
    assert spec.run is not None
    outcome = await spec.run(ctx, {})
    assert outcome["message"].startswith("1 most recent leads:")
    assert "Ravi Kumar, Backend Engineer at Acme (new, recruiter)" in outcome["message"]
    assert s.audit.entries == []
