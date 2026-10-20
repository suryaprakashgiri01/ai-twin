"""observability-alerts T5: the new event sources (AC10): admin auth failures with
`surface: admin`, and `llm_unavailable` when every model in a chain failed."""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from fakeredis import FakeAsyncRedis
from langchain_core.messages import HumanMessage

from app.core.app_factory import create_app
from app.core.config import Settings
from app.services.security_events import recording_exhausted_chains
from llm_orchestrator import AllModelsFailedError, Role, Tier
from tests.conftest import FakeCheck
from tests.fakes import MemoryApiKeyStore
from tests.llm_orchestrator.fakes import Factory, inventory, orchestrator, spec
from tests.public.fakes import MemorySecurityEvents


async def test_admin_auth_failures_are_recorded_with_the_admin_surface() -> None:
    app = create_app(
        Settings(_env_file=None, app_env="test", log_format="json"),
        health_checks=[FakeCheck("mongodb")],
    )
    events = MemorySecurityEvents()
    app.state.resources = SimpleNamespace(
        api_keys=MemoryApiKeyStore(), redis=FakeAsyncRedis(), security_events=events
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        for _ in range(3):
            assert (await c.get("/admin/v1/leads")).status_code == 401
            bad = await c.get("/admin/v1/leads", headers={"X-API-Key": "atw_wrong"})
            assert bad.status_code == 401
    assert [e["kind"] for e in events.events] == ["auth_failed", "auth_failed"]
    assert {e["surface"] for e in events.events} == {"admin"}
    assert sorted(e["rules"][0] for e in events.events) == ["INVALID_API_KEY", "UNAUTHENTICATED"]
    assert events.events[0]["details"] == {"path": "/admin/v1/leads"}
    assert "atw_wrong" not in str(events.events)


async def test_an_exhausted_chain_records_llm_unavailable_once_a_window() -> None:
    events, redis = MemorySecurityEvents(), FakeAsyncRedis()
    orch = orchestrator(
        inventory(spec("a", roles=["judge"]), spec("b", roles=["judge"])),
        Factory({"a": [ConnectionError("down")], "b": [TimeoutError("slow")]}),
        max_attempts=1,
    )
    async with recording_exhausted_chains(events, redis):
        for _ in range(3):
            with pytest.raises(AllModelsFailedError):
                await orch.chat(Role.JUDGE, Tier.PUBLIC, [HumanMessage(content="secret prompt")])
    [event] = events.events
    assert event["kind"] == "llm_unavailable" and event["severity"] == "high"
    assert event["rules"] == ["a:transient_error", "b:transient_error"]
    assert event["details"] == {"role": "judge", "tier": "public", "attempts": 2}
    assert "secret prompt" not in str(event)
