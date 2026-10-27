from __future__ import annotations

import asyncio

import httpx
import pytest

from app.core.config import DEFAULT_VERSION
from app.services.health import QdrantCheck, run_checks
from tests.conftest import FakeCheck, make_app


async def _get(path: str, checks: list[FakeCheck]) -> httpx.Response:
    transport = httpx.ASGITransport(app=make_app(checks))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        return await c.get(path)


async def test_liveness_returns_version(client: httpx.AsyncClient) -> None:
    response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": DEFAULT_VERSION}


async def test_readiness_ok_when_all_checks_pass() -> None:
    response = await _get("/readyz", [FakeCheck("mongodb"), FakeCheck("redis")])
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["checks"] == {"mongodb": "ok", "redis": "ok"}


async def test_readiness_503_names_the_failing_dependency() -> None:
    response = await _get("/readyz", [FakeCheck("mongodb"), FakeCheck("redis", fail=True)])
    assert response.status_code == 503
    assert response.json()["checks"] == {"mongodb": "ok", "redis": "unavailable"}


async def test_slow_check_times_out_as_unavailable() -> None:
    class Slow:
        name = "slow"

        async def ping(self) -> None:
            await asyncio.sleep(5)

    results = await run_checks([Slow()], timeout_seconds=0.01)
    assert results == {"slow": "unavailable"}


@pytest.mark.parametrize("status", [200, 503])
async def test_qdrant_check_uses_readyz(status: int, monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/readyz"
        return httpx.Response(status)

    real_client = httpx.AsyncClient

    def fake_client(**kwargs: object) -> httpx.AsyncClient:
        return real_client(transport=httpx.MockTransport(handler))

    monkeypatch.setattr("app.services.health.httpx.AsyncClient", fake_client)
    results = await run_checks([QdrantCheck("http://qdrant/", 1.0)], timeout_seconds=1.0)
    assert results == {"qdrant": "ok" if status == 200 else "unavailable"}
