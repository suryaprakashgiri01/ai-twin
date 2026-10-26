from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import httpx
import pytest
from fastapi import FastAPI

from app.auth.keys import KEY_PREFIX, generate_key, hash_key, issue_key
from app.core.app_factory import create_app
from app.core.config import Settings
from app.db.documents import Scope
from tests.conftest import FakeCheck
from tests.envelope import items, result
from tests.fakes import MemoryApiKeyStore, MemoryAuditLog, MemoryOverrideStore


@dataclass
class Fakes:
    api_keys: MemoryApiKeyStore
    overrides: MemoryOverrideStore
    audit: MemoryAuditLog


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 1, 1, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


def _app(profile: Literal["vault", "public"] = "vault") -> tuple[FastAPI, Fakes, Clock]:
    settings = Settings(
        _env_file=None, app_env="test", log_format="json", deployment_profile=profile
    )
    app = create_app(settings, health_checks=[FakeCheck("mongodb")])
    fakes = Fakes(MemoryApiKeyStore(), MemoryOverrideStore(), MemoryAuditLog())
    app.state.resources = fakes
    clock = Clock()
    app.state.clock = clock
    return app, fakes, clock


async def _client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
async def setup() -> AsyncIterator[tuple[httpx.AsyncClient, Fakes, Clock]]:
    app, fakes, clock = _app()
    async for c in _client(app):
        yield c, fakes, clock


async def _key(fakes: Fakes, *scopes: Scope, name: str = "k") -> str:
    return (await issue_key(fakes.api_keys, name, scopes))[1]


def test_key_shape_and_hash() -> None:
    key = generate_key()
    assert key.startswith(KEY_PREFIX)
    assert len(key) == len(KEY_PREFIX) + 43  # 32 bytes, URL-safe base64, unpadded
    assert hash_key(key) == hash_key(key) != key
    assert len(hash_key(key)) == 64


async def test_issue_key_rejects_unknown_or_empty_scopes() -> None:
    store = MemoryApiKeyStore()
    with pytest.raises(ValueError):
        await issue_key(store, "x", [])
    with pytest.raises(ValueError):
        await issue_key(store, "x", ["root"])  # type: ignore[list-item]


async def test_plaintext_key_is_never_stored_or_logged(
    setup: tuple[httpx.AsyncClient, Fakes, Clock], capsys: pytest.CaptureFixture[str]
) -> None:
    client, fakes, _ = setup
    key = await _key(fakes, "admin")
    response = await client.get("/admin/v1/whoami", headers={"X-API-Key": key})
    assert response.status_code == 200
    assert key not in json.dumps(fakes.api_keys.rows, default=str)
    assert key not in capsys.readouterr().out


async def test_missing_unknown_revoked_and_wrong_scope(
    setup: tuple[httpx.AsyncClient, Fakes, Clock],
) -> None:
    client, fakes, clock = setup
    leads = await _key(fakes, "leads:write", name="leads")
    url = "/admin/v1/models/overrides"

    missing = await client.get(url)
    unknown = await client.get(url, headers={"X-API-Key": KEY_PREFIX + "nope"})
    wrong = await client.get(url, headers={"X-API-Key": leads})
    assert (missing.status_code, missing.json()["error"]["code"]) == (401, "UNAUTHENTICATED")
    assert (unknown.status_code, unknown.json()["error"]["code"]) == (401, "INVALID_API_KEY")
    assert (wrong.status_code, wrong.json()["error"]["code"]) == (403, "FORBIDDEN")

    await fakes.api_keys.revoke("leads", clock())
    revoked = await client.get("/admin/v1/whoami", headers={"X-API-Key": leads})
    assert (revoked.status_code, revoked.json()["error"]["code"]) == (401, "INVALID_API_KEY")


async def test_whoami_and_last_used_at_once_a_minute(
    setup: tuple[httpx.AsyncClient, Fakes, Clock],
) -> None:
    client, fakes, clock = setup
    key = await _key(fakes, "public:read", name="reader")
    headers = {"X-API-Key": key}
    response = await client.get("/admin/v1/whoami", headers=headers)
    assert result(response) == {"name": "reader", "scopes": ["public:read"]}
    clock.now += timedelta(seconds=30)
    await client.get("/admin/v1/whoami", headers=headers)
    assert len(fakes.api_keys.touches) == 1
    clock.now += timedelta(seconds=31)
    await client.get("/admin/v1/whoami", headers=headers)
    assert len(fakes.api_keys.touches) == 2


async def test_public_profile_mounts_no_admin_routes() -> None:
    app, fakes, _ = _app("public")
    key = await _key(fakes, "admin")
    async for client in _client(app):
        response = await client.get("/admin/v1/whoami", headers={"X-API-Key": key})
        assert response.status_code == 404
    assert not any(getattr(r, "path", "").startswith("/admin") for r in app.routes)


async def test_override_put_is_validated_stored_and_audited(
    setup: tuple[httpx.AsyncClient, Fakes, Clock],
) -> None:
    client, fakes, _ = setup
    headers = {"X-API-Key": await _key(fakes, "admin", name="owner")}
    url = "/admin/v1/models/overrides"

    first = await client.put(f"{url}/groq-gpt-oss-20b", json={"enabled": False}, headers=headers)
    second = await client.put(
        f"{url}/groq-gpt-oss-20b", json={"enabled": True, "max_tier": "public"}, headers=headers
    )
    assert first.status_code == second.status_code == 200
    assert result(second)["maxTier"] == "public"

    listed = await client.get(url, headers=headers)
    assert [o["id"] for o in items(listed)] == ["groq-gpt-oss-20b"]

    assert len(fakes.audit.entries) == 2
    entry: dict[str, Any] = fakes.audit.entries[1]
    assert entry["actor"] == "key:owner"
    assert entry["action"] == "model_override.put"
    assert entry["target"] == "groq-gpt-oss-20b"
    assert entry["before"]["enabled"] is False
    assert entry["after"]["max_tier"] == "public"


@pytest.mark.parametrize(
    ("model_id", "body"),
    [
        ("groq-gpt-oss-20b", {"max_tier": "restricted"}),
        ("groq-gpt-oss-20b", {"max_tier": "secret"}),
        ("groq-gpt-oss-20b", {}),
        ("groq-gpt-oss-20b", {"chain": ["mock"]}),
        ("groq-gpt-oss-20b", {"enabled": True, "surprise": 1}),
        ("groq-gpt-oss-20b", {"max_tier": "confidential"}),  # above the file's ceiling
        ("not-in-inventory", {"enabled": False}),
        ("role:classifier", {"enabled": True}),
        ("role:classifier", {"chain": []}),
        ("role:classifier", {"chain": ["groq-gpt-oss-120b"]}),  # does not serve the role
        ("role:owner_chat", {"chain": ["mock"]}),  # no such role
    ],
)
async def test_override_put_rejects_bad_shapes(
    setup: tuple[httpx.AsyncClient, Fakes, Clock], model_id: str, body: dict[str, Any]
) -> None:
    client, fakes, _ = setup
    headers = {"X-API-Key": await _key(fakes, "admin")}
    response = await client.put(
        f"/admin/v1/models/overrides/{model_id}", json=body, headers=headers
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_INPUT"
    assert fakes.audit.entries == []
    assert fakes.overrides.rows == {}


async def test_role_chain_override(setup: tuple[httpx.AsyncClient, Fakes, Clock]) -> None:
    client, fakes, _ = setup
    headers = {"X-API-Key": await _key(fakes, "admin")}
    response = await client.put(
        "/admin/v1/models/overrides/role:classifier",
        json={"chain": ["mock", "ollama-local"]},
        headers=headers,
    )
    assert response.status_code == 200
    assert result(response)["chain"] == ["mock", "ollama-local"]
