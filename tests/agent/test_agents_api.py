"""Owner-console T4: per-agent model settings, overridden live and reset to the file."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from types import SimpleNamespace

import httpx
import pytest

from app.agents.models import AgentModel, AgentModels
from app.auth.keys import issue_key
from app.core.app_factory import create_app
from app.core.config import Settings
from app.core.llm import provider_secrets
from app.exceptions import AppError
from app.services.agent_settings import AgentSettingsIn, AgentSettingsService
from app.services.model_status import ModelStatusChecker
from llm_orchestrator import Tier
from tests.conftest import FakeCheck
from tests.envelope import result
from tests.fakes import (
    MemoryAgentOverrideStore,
    MemoryApiKeyStore,
    MemoryAuditLog,
    MemoryOverrideStore,
)
from tests.llm_orchestrator.fakes import inventory, spec


def models() -> AgentModels:
    inv = inventory(
        spec("cloud", provider="groq", roles=["generator"], max_tier="private"),
        spec("local", provider="ollama", roles=["generator"], max_tier="confidential"),
    )
    return AgentModels.build({"prep": AgentModel(prefer="quality", need="complex")}, inv)


async def test_an_override_applies_to_the_next_pick_and_resets_to_the_file() -> None:
    agents = models()
    service = AgentSettingsService(agents, MemoryAgentOverrideStore())
    live = agents.live("prep")
    assert live.pick(Tier.PRIVATE) == (None, "quality")
    pinned = await service.put("prep", AgentSettingsIn(model="cloud", confidential_model="local"))
    assert (pinned.source, pinned.model, pinned.updated_at is not None) == (
        "override",
        "cloud",
        True,
    )
    assert live.pick(Tier.PRIVATE) == ("cloud", None)
    assert live.pick(Tier.CONFIDENTIAL) == ("local", None)
    assert await live.need_for("thanks", Tier.PRIVATE, None) is None  # the override has no need
    reset = await service.reset("prep")
    assert (reset.source, reset.prefer, reset.need) == ("yaml", "quality", "complex")
    assert live.pick(Tier.PRIVATE) == (None, "quality")
    assert service.get("owner").source == "default"
    assert [a.name for a in service.list()] == ["owner", "prep", "super", "intake"]


async def test_a_cloud_or_unknown_model_is_refused_and_bad_rows_are_skipped() -> None:
    store = MemoryAgentOverrideStore()
    service = AgentSettingsService(models(), store)
    with pytest.raises(AppError, match="cloud model"):
        await service.put("super", AgentSettingsIn(confidential_model="cloud"))
    with pytest.raises(AppError, match=r"not in config/models\.yaml"):
        await service.put("super", AgentSettingsIn(model="nope"))
    assert store.rows == {}
    await store.put("owner", AgentModel(confidential_model="cloud"))  # written before a change
    await store.put("intake", AgentModel(need="moderate"))
    fresh = AgentSettingsService(models(), store)
    await fresh.load()
    assert fresh.get("owner").source == "default"
    assert fresh.get("intake").source == "override"


@dataclass
class Api:
    client: httpx.AsyncClient
    headers: dict[str, str]
    audit: MemoryAuditLog


@pytest.fixture
async def api() -> AsyncIterator[Api]:
    app = create_app(
        Settings(_env_file=None, app_env="test", log_format="json"),
        health_checks=[FakeCheck("mongodb")],
    )
    keys, audit = MemoryApiKeyStore(), MemoryAuditLog()
    app.state.resources = SimpleNamespace(
        api_keys=keys, audit=audit, overrides=MemoryOverrideStore()
    )
    app.state.agent_settings = AgentSettingsService(models(), MemoryAgentOverrideStore())
    # No real Ollama or provider is called from a unit test.
    offline = httpx.MockTransport(lambda _: httpx.Response(503))
    app.state.model_status = ModelStatusChecker(
        app.state.settings, provider_secrets(app.state.settings), offline
    )
    key = (await issue_key(keys, "owner", ["admin"]))[1]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield Api(client, {"X-API-Key": key}, audit)


async def test_agent_routes_list_put_reset_and_audit(api: Api) -> None:
    listed = result(await api.client.get("/admin/v1/agents", headers=api.headers))
    prep = next(a for a in listed if a["name"] == "prep")
    assert (prep["source"], prep["need"], prep["endpoint"]) == ("yaml", "complex", "/admin/v1/prep")
    url = "/admin/v1/agents/prep"
    put = await api.client.put(url, json={"model": "cloud", "prefer": "fast"}, headers=api.headers)
    assert put.status_code == 200 and result(put)["source"] == "override"
    refused = await api.client.put(url, json={"confidentialModel": "cloud"}, headers=api.headers)
    assert refused.status_code == 422 and refused.json()["error"]["code"] == "INVALID_INPUT"
    bad = await api.client.put(url, json={"prefer": "cheapest"}, headers=api.headers)
    assert bad.status_code == 422
    unknown = await api.client.put("/admin/v1/agents/nobody", json={}, headers=api.headers)
    assert unknown.status_code == 422
    reset = await api.client.delete(url, headers=api.headers)
    assert result(reset)["source"] == "yaml"
    actions = [(e["action"], e["target"]) for e in api.audit.entries]
    assert actions == [("agent_settings.put", "prep"), ("agent_settings.reset", "prep")]
    assert api.audit.entries[0]["after"]["model"] == "cloud"
    assert (await api.client.get("/admin/v1/agents")).status_code == 401


async def test_model_list_for_the_picker_hides_provider_names(api: Api) -> None:
    listed = result(await api.client.get("/admin/v1/models", headers=api.headers))
    assert listed and {"id", "status", "provider", "maxTier", "enabled", "local"} <= set(listed[0])
    statuses = {m["status"] for m in listed}
    assert statuses <= {"active", "disabled", "no key", "not pulled", "unreachable"}
    assert all("model" not in m for m in listed)
    assert all(m["local"] == (m["provider"] in {"ollama", "mock"}) for m in listed)
