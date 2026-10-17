"""Public-agent T1: profile gating, the public:read scope, visitor identity, rate limits (AC2-3)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any, Literal

import httpx
import pytest
from fakeredis import FakeAsyncRedis
from fastapi import FastAPI
from pydantic import SecretStr

from app.auth.keys import issue_key
from app.auth.visitor import Visitor
from app.core.app_factory import create_app
from app.core.config import Settings
from app.services.public_chat import PublicChatRequest, PublicChatResponse, PublicPrepared
from app.services.rate_limit import Limits, RateLimiter
from tests.conftest import FakeCheck
from tests.fakes import MemoryApiKeyStore, MemoryAuditLog, MemoryOverrideStore
from tests.public.fakes import MemorySecurityEvents


class EchoChat:
    """Stands in for the public chat service: records which visitor each message came from."""

    def __init__(self) -> None:
        self.visitors: list[str] = []

    async def prepare(self, request: PublicChatRequest, visitor: Visitor) -> PublicPrepared:
        self.visitors.append(visitor.hash)
        return PublicPrepared(request, visitor, "c" * 32, single_turn=True)

    async def sse(self, prepared: PublicPrepared) -> AsyncIterator[str]:
        yield "event: done\ndata: {}\n\n"

    async def answer(self, prepared: PublicPrepared) -> PublicChatResponse:
        return PublicChatResponse(
            conversation_id=prepared.conversation_id, intent="greeting", answer="hi"
        )


def public_app(
    profile: Literal["vault", "public"] = "public", **overrides: Any
) -> tuple[FastAPI, MemoryApiKeyStore, EchoChat]:
    settings = Settings(
        _env_file=None,
        app_env="test",
        log_format="json",
        deployment_profile=profile,
        **{"visitor_hash_salt": SecretStr("test-salt"), **overrides},
    )
    app = create_app(settings, health_checks=[FakeCheck("mongodb")])
    keys = MemoryApiKeyStore()
    app.state.resources = SimpleNamespace(
        api_keys=keys,
        audit=MemoryAuditLog(),
        overrides=MemoryOverrideStore(),
        redis=FakeAsyncRedis(decode_responses=True),
        security_events=MemorySecurityEvents(),
    )
    chat = EchoChat()
    app.state.public_chat = chat
    return app, keys, chat


async def client_for(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def _post(
    client: httpx.AsyncClient, key: str, address: str | None = "203.0.113.7", **params: Any
) -> httpx.Response:
    headers = {"X-API-Key": key}
    if address is not None:
        headers["X-Visitor-Address"] = address
    return await client.post(
        "/v1/chat", json={"message": "hello"}, headers=headers, params=params or None
    )


async def test_public_profile_has_only_public_and_health_routes() -> None:
    app, keys, _ = public_app()
    admin = (await issue_key(keys, "owner", ["admin"]))[1]
    paths = set(app.openapi()["paths"])
    assert not any(p.startswith("/admin") for p in paths)
    assert {p for p in paths if not p.startswith("/v1/")} == {"/healthz", "/readyz"}
    async for client in client_for(app):
        whoami = await client.get("/admin/v1/whoami", headers={"X-API-Key": admin})
        assert whoami.status_code == 404
        assert (await client.post("/admin/v1/chat", json={})).status_code == 404


async def test_public_routes_need_public_read() -> None:
    app, keys, _ = public_app()
    leads = (await issue_key(keys, "leads", ["leads:write"]))[1]
    reader = (await issue_key(keys, "portfolio", ["public:read"]))[1]
    async for client in client_for(app):
        missing = await client.post("/v1/chat", json={"message": "hello"})
        assert missing.status_code == 401
        assert missing.json()["error"]["code"] == "AUTH_REQUIRED"
        wrong = await _post(client, "atw_not-a-key")
        assert wrong.status_code == 401
        forbidden = await _post(client, leads)
        assert forbidden.status_code == 403
        assert forbidden.json()["error"]["code"] == "FORBIDDEN"
        assert (await _post(client, reader)).status_code == 200
        assert (await _post(client, reader, stream="false")).json()["answer"] == "hi"


async def test_vault_profile_mounts_public_routes_too() -> None:
    app, keys, _ = public_app("vault")
    reader = (await issue_key(keys, "portfolio", ["public:read"]))[1]
    async for client in client_for(app):
        assert (await _post(client, reader)).status_code == 200


async def test_unknown_fields_cannot_widen_the_request() -> None:
    app, keys, _ = public_app()
    reader = (await issue_key(keys, "portfolio", ["public:read"]))[1]
    async for client in client_for(app):
        for extra in ({"max_tier": "confidential"}, {"collections": ["kb_private"]}):
            response = await client.post(
                "/v1/chat",
                json={"message": "hello", **extra},
                headers={"X-API-Key": reader, "X-Visitor-Address": "203.0.113.7"},
            )
            assert response.status_code == 422


async def test_ninth_message_in_a_minute_is_429_with_retry_after() -> None:
    app, keys, chat = public_app()
    reader = (await issue_key(keys, "portfolio", ["public:read"]))[1]
    async for client in client_for(app):
        for _ in range(8):
            assert (await _post(client, reader)).status_code == 200
        ninth = await _post(client, reader)
        assert ninth.status_code == 429
        assert ninth.json()["error"]["code"] == "RATE_LIMITED"
        assert ninth.json()["error"]["details"] == {"window": "minute"}
        assert 1 <= int(ninth.headers["Retry-After"]) <= 60
        # Another visitor is not affected.
        assert (await _post(client, reader, address="198.51.100.9")).status_code == 200
    assert len(chat.visitors) == 9
    assert len(set(chat.visitors)) == 2
    assert "203.0.113.7" not in chat.visitors  # only the salted hash travels on


async def test_global_daily_cap_applies_across_visitors() -> None:
    app, keys, _ = public_app(public_daily_message_cap=3)
    reader = (await issue_key(keys, "portfolio", ["public:read"]))[1]
    async for client in client_for(app):
        for i in range(3):
            assert (await _post(client, reader, address=f"192.0.2.{i + 1}")).status_code == 200
        capped = await _post(client, reader, address="192.0.2.99")
        assert capped.status_code == 429
        assert capped.json()["error"]["details"] == {"window": "global"}
        assert int(capped.headers["Retry-After"]) >= 1


async def test_visitor_header_trusted_only_from_public_read_keys() -> None:
    app, keys, chat = public_app()
    admin = (await issue_key(keys, "owner", ["admin"]))[1]
    reader = (await issue_key(keys, "portfolio", ["public:read"]))[1]
    async for client in client_for(app):
        await _post(client, admin, address="192.0.2.1")
        await _post(client, admin, address="192.0.2.2")
        await _post(client, reader, address="192.0.2.1")
        await _post(client, reader, address="192.0.2.2")
        await _post(client, reader, address=None)
        bad = await _post(client, reader, address="not-an-ip")
        assert bad.status_code == 422
    admin_a, admin_b, reader_a, reader_b, reader_none = chat.visitors
    assert admin_a == admin_b  # an admin key is one visitor, whatever it forwards
    assert reader_a != reader_b
    assert reader_none not in (reader_a, reader_b)


async def test_missing_salt_is_a_dependency_error() -> None:
    app, keys, _ = public_app(visitor_hash_salt=SecretStr(""))
    reader = (await issue_key(keys, "portfolio", ["public:read"]))[1]
    async for client in client_for(app):
        assert (await _post(client, reader)).status_code == 503


async def test_chat_without_the_service_is_unavailable() -> None:
    app, keys, _ = public_app()
    del app.state.public_chat
    reader = (await issue_key(keys, "portfolio", ["public:read"]))[1]
    async for client in client_for(app):
        assert (await _post(client, reader)).status_code == 503


class Clock:
    def __init__(self, now: float = 1_800_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def limiter_clock() -> tuple[RateLimiter, Clock]:
    clock = Clock()
    redis = FakeAsyncRedis(decode_responses=True)
    limits = Limits(per_minute=2, per_day=3, global_per_day=100)
    return RateLimiter(redis, limits, clock=clock), clock


async def test_minute_window_slides(limiter_clock: tuple[RateLimiter, Clock]) -> None:
    limiter, clock = limiter_clock
    assert (await limiter.hit("v")).allowed
    clock.now += 20
    assert (await limiter.hit("v")).allowed
    refused = await limiter.hit("v")
    assert not refused.allowed and refused.window == "minute" and refused.retry_after == 40
    clock.now += 41
    assert (await limiter.hit("v")).allowed  # the first message left the window


async def test_day_window_and_refusals_are_not_counted(
    limiter_clock: tuple[RateLimiter, Clock],
) -> None:
    limiter, clock = limiter_clock
    for _ in range(3):
        assert (await limiter.hit("v")).allowed
        clock.now += 61
    refused = await limiter.hit("v")
    assert refused.window == "day" and refused.retry_after > 3600
    for _ in range(5):
        assert not (await limiter.hit("v")).allowed
    clock.now += 86_400
    assert (await limiter.hit("v")).allowed
