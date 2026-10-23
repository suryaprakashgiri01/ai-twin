"""console-polish P1: saved conversation starters per agent."""

from __future__ import annotations

from types import SimpleNamespace

import httpx

from app.auth.keys import issue_key
from app.core.app_factory import create_app
from app.core.config import Settings
from tests.conftest import FakeCheck
from tests.envelope import result
from tests.fakes import MemoryApiKeyStore, MemoryAuditLog, MemoryOverrideStore, MemoryStarterStore


async def test_starters_add_list_by_agent_and_delete() -> None:
    app = create_app(
        Settings(_env_file=None, app_env="test", log_format="json"),
        health_checks=[FakeCheck("mongodb")],
    )
    keys, audit, store = MemoryApiKeyStore(), MemoryAuditLog(), MemoryStarterStore()
    app.state.resources = SimpleNamespace(
        api_keys=keys, audit=audit, overrides=MemoryOverrideStore(), starters=store
    )
    owner = {"X-API-Key": (await issue_key(keys, "owner", ["admin"]))[1]}
    visitor = {"X-API-Key": (await issue_key(keys, "site", ["public:read"]))[1]}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        made = await client.post(
            "/admin/v1/starters", json={"agent": "prep", "text": "Pitch me for Acme"}, headers=owner
        )
        assert made.status_code == 201 and result(made)["agent"] == "prep"
        await client.post("/admin/v1/starters", json={"agent": "ask", "text": "x"}, headers=owner)
        prep = result(await client.get("/admin/v1/starters?agent=prep", headers=owner))
        assert [s["text"] for s in prep] == ["Pitch me for Acme"]
        assert len(result(await client.get("/admin/v1/starters", headers=owner))) == 2
        bad = await client.post(
            "/admin/v1/starters", json={"agent": "nobody", "text": "x"}, headers=owner
        )
        assert bad.status_code == 422
        empty = await client.post(
            "/admin/v1/starters", json={"agent": "ask", "text": ""}, headers=owner
        )
        assert empty.status_code == 422
        sid = result(made)["id"]
        assert (await client.delete(f"/admin/v1/starters/{sid}", headers=owner)).status_code == 200
        assert (await client.delete(f"/admin/v1/starters/{sid}", headers=owner)).status_code == 404
        assert (await client.get("/admin/v1/starters", headers=visitor)).status_code == 403
    assert [e["action"] for e in audit.entries] == ["starter.add", "starter.add", "starter.delete"]
