"""Admin memory routes: create active, accept / edit / archive / delete, audited by key."""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest

from app.auth.keys import issue_key
from app.services.memory import MemoryService
from llm_orchestrator import Tier
from tests.memory.fakes import FakeAudit, FakeMemoryStore
from tests.test_admin_api import _app, _client


@pytest.fixture
async def api() -> AsyncIterator[
    tuple[httpx.AsyncClient, dict[str, str], MemoryService, FakeAudit]
]:
    app, fakes, _ = _app()
    audit = FakeAudit()
    app.state.memory = MemoryService(FakeMemoryStore(), audit)
    key = (await issue_key(fakes.api_keys, "owner", ["admin"]))[1]
    async for client in _client(app):
        yield client, {"X-API-Key": key}, app.state.memory, audit


async def test_memory_crud_is_audited(
    api: tuple[httpx.AsyncClient, dict[str, str], MemoryService, FakeAudit],
) -> None:
    client, headers, svc, audit = api
    created = await client.post(
        "/admin/v1/memories",
        json={"text": "Use British spelling", "kind": "instruction"},
        headers=headers,
    )
    assert created.status_code == 201 and created.json()["status"] == "active"
    again = await client.post(
        "/admin/v1/memories", json={"text": "use british spelling"}, headers=headers
    )
    assert again.status_code == 200 and again.json()["uses"] == 2

    from app.services.digest import MemoryCandidate

    await svc.learn(
        [MemoryCandidate(kind="preference", text="Concise bullets")],
        owner_text="Always give concise bullets please",
        conversation_id="c1",
        tier=Tier.PRIVATE,
    )
    pending = (await client.get("/admin/v1/memories?status=pending", headers=headers)).json()
    mid = pending[0]["id"]
    accepted = await client.patch(
        f"/admin/v1/memories/{mid}", json={"status": "active"}, headers=headers
    )
    assert accepted.json()["status"] == "active"
    edited = await client.patch(
        f"/admin/v1/memories/{mid}", json={"text": "Short bullets"}, headers=headers
    )
    assert edited.json()["text"] == "Short bullets"
    archived = await client.patch(
        f"/admin/v1/memories/{mid}", json={"status": "archived"}, headers=headers
    )
    assert archived.json()["status"] == "archived"
    assert (await client.delete(f"/admin/v1/memories/{mid}", headers=headers)).status_code == 204
    assert (await client.delete(f"/admin/v1/memories/{mid}", headers=headers)).status_code == 404
    listed = (await client.get("/admin/v1/memories", headers=headers)).json()
    assert [m["text"] for m in listed] == ["Use British spelling"]
    actions = [(actor, action) for actor, action, _, _ in audit.entries]
    assert actions == [
        ("key:owner", "memory.add"),
        ("key:owner", "memory.bump"),
        ("agent:digest", "memory.learn"),
        ("key:owner", "memory.accept"),
        ("key:owner", "memory.edit"),
        ("key:owner", "memory.archive"),
        ("key:owner", "memory.delete"),
    ]


async def test_memory_routes_validate_and_need_admin(
    api: tuple[httpx.AsyncClient, dict[str, str], MemoryService, FakeAudit],
) -> None:
    client, headers, _, _ = api
    too_long = await client.post("/admin/v1/memories", json={"text": "x" * 201}, headers=headers)
    assert too_long.status_code == 422
    bad_kind = await client.post(
        "/admin/v1/memories", json={"text": "x", "kind": "fact"}, headers=headers
    )
    assert bad_kind.status_code == 422
    assert (await client.get("/admin/v1/memories")).status_code == 401
