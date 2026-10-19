"""Ingestion T1: admin overrides reach the orchestrator on its next call, with no restart."""

from __future__ import annotations

from types import SimpleNamespace

import httpx

from app.auth.keys import issue_key
from app.core.app_factory import create_app
from app.core.config import Settings
from app.services.model_overrides import OrchestratorOverrides, OverrideIn
from llm_orchestrator import Role, Tier
from tests.conftest import FakeCheck
from tests.fakes import MemoryApiKeyStore, MemoryAuditLog, MemoryOverrideStore


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        app_env="test",
        log_format="json",
        ollama_base_url="http://127.0.0.1:11434",
        llm_mock_enabled=True,
    )


async def _selected(app: object) -> list[str]:
    orchestrator = app.state.llm.orchestrator  # type: ignore[attr-defined]
    selection = await orchestrator.select(Role.CLASSIFIER, Tier.CONFIDENTIAL)
    return [m.id for m in selection.models]


async def test_override_put_changes_the_next_selection() -> None:
    app = create_app(_settings(), health_checks=[FakeCheck("mongodb")])
    fakes = SimpleNamespace(
        api_keys=MemoryApiKeyStore(), overrides=MemoryOverrideStore(), audit=MemoryAuditLog()
    )
    app.state.resources = fakes
    key = (await issue_key(fakes.api_keys, "owner", ["admin"]))[1]
    headers = {"X-API-Key": key}

    assert await _selected(app) == ["ollama-local", "mock"]
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        off = await client.put(
            "/admin/v1/models/overrides/ollama-local", json={"enabled": False}, headers=headers
        )
        assert off.status_code == 200
        assert await _selected(app) == ["mock"]

        chain = await client.put(
            "/admin/v1/models/overrides/role:classifier",
            json={"chain": ["mock", "ollama-local"]},
            headers=headers,
        )
        assert chain.status_code == 200
        on = await client.put(
            "/admin/v1/models/overrides/ollama-local", json={"enabled": True}, headers=headers
        )
        assert on.status_code == 200
    assert await _selected(app) == ["mock", "ollama-local"]


async def test_adapter_without_storage_has_no_overrides() -> None:
    adapter = OrchestratorOverrides(lambda: None)
    assert await adapter.model_overrides() == {}
    assert await adapter.role_chains() == {}


async def test_adapter_skips_rows_for_unknown_roles() -> None:
    store = MemoryOverrideStore()
    await store.put("role:nope", OverrideIn(chain=["mock"]))
    await store.put("mock", OverrideIn(max_tier="private"))
    adapter = OrchestratorOverrides(lambda: store)
    assert await adapter.role_chains() == {}
    overrides = await adapter.model_overrides()
    assert overrides["mock"].max_tier == Tier.PRIVATE


async def test_readyz_works_before_storage_opens() -> None:
    app = create_app(_settings(), health_checks=[FakeCheck("mongodb")])
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/readyz")
    assert response.json()["models"]["classifier"] == 2
