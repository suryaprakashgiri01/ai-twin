"""observability-alerts T4: `/metrics` (AC6): open in the vault, a bearer token in the public
profile (404 unset, 401 wrong), the listed series, and labels that never carry ids or text."""

from __future__ import annotations

import re

import httpx
import pytest
from langchain_core.messages import HumanMessage
from prometheus_client.parser import text_string_to_metric_families

from app.core.app_factory import create_app
from app.core.config import Settings
from app.observability.metrics import REGISTRY, install_llm_metrics
from llm_orchestrator import AllModelsFailedError, Role, Tier
from tests.conftest import FakeCheck
from tests.llm_orchestrator.fakes import Factory, inventory, orchestrator, spec
from tests.public.fakes import make_public

TOKEN = "metrics-test-token"
SERIES = (
    "aitwin_http_requests_total",
    "aitwin_http_request_duration_seconds",
    "aitwin_llm_attempts_total",
    "aitwin_llm_attempt_duration_seconds",
    "aitwin_llm_tokens_total",
    "aitwin_llm_chain_exhausted_total",
    "aitwin_guard_decisions_total",
    "aitwin_scope_intents_total",
    "aitwin_answer_cache_lookups_total",
    "aitwin_ingestion_jobs_total",
    "aitwin_leads_total",
    "aitwin_emails_total",
    "aitwin_security_events_total",
    "aitwin_alert_emails_total",
)


def client_for(**settings: object) -> httpx.AsyncClient:
    app = create_app(
        Settings(_env_file=None, app_env="test", log_format="json", **settings),  # type: ignore[arg-type]
        health_checks=[FakeCheck("mongodb")],
    )
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def value(name: str, **labels: str) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


async def test_vault_metrics_are_open_and_count_requests_by_route_template() -> None:
    before = value("aitwin_http_requests_total", route="<unmatched>", method="GET", status="404")
    async with client_for() as c:
        await c.get("/healthz")
        await c.get("/conversations/0123456789abcdef0123456789abcdef")
        response = await c.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    body = response.text
    assert 'aitwin_http_requests_total{method="GET",route="/healthz",status="200"}' in body
    assert "0123456789abcdef" not in body
    after = value("aitwin_http_requests_total", route="<unmatched>", method="GET", status="404")
    assert after == before + 1


async def test_public_metrics_are_404_without_a_token() -> None:
    async with client_for(deployment_profile="public") as c:
        response = await c.get("/metrics", headers={"Authorization": "Bearer anything"})
    assert response.status_code == 404


@pytest.mark.parametrize(
    "header", [None, "Bearer wrong", f"Basic {TOKEN}", f"Bearer {TOKEN}x", "Bearer"]
)
async def test_public_metrics_refuse_a_wrong_token(header: str | None) -> None:
    headers = {"Authorization": header} if header else {}
    async with client_for(deployment_profile="public", metrics_token=TOKEN) as c:
        response = await c.get("/metrics", headers=headers)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTH_INVALID"


async def test_public_metrics_with_the_token() -> None:
    async with client_for(deployment_profile="public", metrics_token=TOKEN) as c:
        response = await c.get("/metrics", headers={"Authorization": f"Bearer {TOKEN}"})
    assert response.status_code == 200
    assert "aitwin_http_requests_total" in response.text


async def test_model_calls_and_exhausted_chains_are_counted() -> None:
    install_llm_metrics()
    install_llm_metrics()  # idempotent: one count per call
    factory = Factory({"ok": ["fine"], "bad": [ConnectionError("down")]})
    orch = orchestrator(
        inventory(spec("ok", roles=["judge"]), spec("bad", roles=["classifier"])),
        factory,
        max_attempts=1,
    )
    labels = {"model": "ok", "role": "judge", "tier": "public", "outcome": "ok"}
    before = value("aitwin_llm_attempts_total", **labels)
    tokens = value("aitwin_llm_tokens_total", model="ok", role="judge", direction="input")
    await orch.chat(Role.JUDGE, Tier.PUBLIC, [HumanMessage(content="hi")])
    assert value("aitwin_llm_attempts_total", **labels) == before + 1
    after = value("aitwin_llm_tokens_total", model="ok", role="judge", direction="input")
    assert after == tokens + 3

    exhausted = value("aitwin_llm_chain_exhausted_total", role="classifier", tier="public")
    with pytest.raises(AllModelsFailedError):
        await orch.chat(Role.CLASSIFIER, Tier.PUBLIC, [HumanMessage(content="hi")])
    assert value("aitwin_llm_chain_exhausted_total", role="classifier", tier="public") == (
        exhausted + 1
    )


async def test_public_turn_counts_guards_intents_and_cache() -> None:
    public = make_public()
    allowed = value("aitwin_guard_decisions_total", layer="input:all", outcome="allowed")
    misses = value("aitwin_answer_cache_lookups_total", result="miss")
    hits = value("aitwin_answer_cache_lookups_total", result="hit")
    await public.ask("What did you build?")
    await public.ask("What did you build?")
    assert value("aitwin_guard_decisions_total", layer="input:all", outcome="allowed") == (
        allowed + 1
    )
    assert value("aitwin_answer_cache_lookups_total", result="miss") == misses + 1
    assert value("aitwin_answer_cache_lookups_total", result="hit") == hits + 1
    assert value("aitwin_scope_intents_total", intent="about_owner") >= 1


async def test_every_series_is_exposed_and_no_label_carries_an_id_or_text() -> None:
    async with client_for() as c:
        await c.get("/healthz")
        body = (await c.get("/metrics")).text
    families = {f.name for f in text_string_to_metric_families(body)}
    for name in SERIES:
        assert name.removesuffix("_total") in families, name
    hexid = re.compile(r"[0-9a-f]{24,}")
    for family in text_string_to_metric_families(body):
        for sample in family.samples:
            for label, label_value in sample.labels.items():
                assert not hexid.search(label_value), (family.name, label, label_value)
                assert "@" not in label_value and len(label_value) <= 64


async def test_route_labels_keep_their_router_prefixes() -> None:
    async with client_for() as c:
        await c.get("/admin/v1/leads")
        body = (await c.get("/metrics")).text
    assert 'route="/admin/v1/leads"' in body
    assert 'route="/leads"' not in body
