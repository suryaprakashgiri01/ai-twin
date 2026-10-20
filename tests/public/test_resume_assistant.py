"""Public-agent T7: /v1/resume (vault blob or provisioned file) and /v1/assistant (AC10)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal

import httpx
import pytest
from beanie import PydanticObjectId
from cryptography.fernet import Fernet
from fakeredis import FakeAsyncRedis
from pydantic import SecretStr

from app.auth.keys import issue_key
from app.core.app_factory import create_app
from app.core.config import Settings
from app.services.profile import ProfileIn
from app.services.resume import FileResume, VaultResume, resume_provider
from app.storage.blobs import BlobStore
from tests.conftest import FakeCheck
from tests.envelope import result
from tests.fakes import MemoryApiKeyStore
from tests.ingestion.fakes import MemoryProfileStore

PDF = b"%PDF-1.7\n% fake persona resume\n%%EOF\n"


@pytest.fixture
def pdf(tmp_path: Path) -> Path:
    path = tmp_path / "resume.pdf"
    path.write_bytes(PDF)
    return path


async def _client(
    profile: Literal["vault", "public"], provider: Any, **overrides: Any
) -> AsyncIterator[tuple[httpx.AsyncClient, str, FakeAsyncRedis, MemoryApiKeyStore]]:
    settings = Settings(
        _env_file=None,
        app_env="test",
        log_format="json",
        deployment_profile=profile,
        visitor_hash_salt=SecretStr("salt"),
        **overrides,
    )
    app = create_app(settings, health_checks=[FakeCheck("mongodb")])
    keys = MemoryApiKeyStore()
    redis = FakeAsyncRedis(decode_responses=True)
    store = MemoryProfileStore()
    await store.put(ProfileIn(availability="open to backend roles"), None)
    app.state.resources = SimpleNamespace(api_keys=keys, redis=redis, profile=store)
    if provider is not None:
        app.state.resume = provider
    key = (await issue_key(keys, "portfolio", ["public:read"]))[1]
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, key, redis, keys


async def test_public_profile_serves_the_provisioned_pdf(pdf: Path) -> None:
    async for client, key, redis, _ in _client("public", FileResume(pdf)):
        headers = {"X-API-Key": key, "X-Visitor-Address": "203.0.113.7"}
        response = await client.get("/v1/resume", headers=headers)
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/pdf"
        assert response.content == PDF
        assert 'filename="resume.pdf"' in response.headers["content-disposition"]
        await client.get("/v1/resume", headers=headers)
        downloads = [k for k in await redis.keys("resume:downloads:*")]
        assert len(downloads) == 1 and await redis.get(downloads[0]) == "2"
        [visitors] = await redis.keys("resume:visitors:*")
        assert await redis.pfcount(visitors) == 1
        dump = str([await redis.dump(k) for k in await redis.keys("*")])
        assert "203.0.113.7" not in dump


async def test_no_resume_is_404(tmp_path: Path) -> None:
    for provider in (FileResume(None), FileResume(tmp_path / "missing.pdf"), None):
        async for client, key, _, _ in _client("public", provider):
            response = await client.get("/v1/resume", headers={"X-API-Key": key})
            assert response.status_code == 404
            assert response.json()["error"]["code"] == "NOT_FOUND"


async def test_vault_serves_the_profile_resume_blob(tmp_path: Path) -> None:
    blobs = BlobStore(tmp_path / "blobs", Fernet.generate_key())
    ref = blobs.put(PDF, "application/pdf")
    store = MemoryProfileStore()
    provider = VaultResume(store, blobs)
    assert not await provider.available() and await provider.open() is None
    await store.put(ProfileIn(), (PydanticObjectId(), ref))
    assert await provider.available()
    async for client, key, _, _ in _client("vault", provider):
        response = await client.get("/v1/resume", headers={"X-API-Key": key})
        assert response.status_code == 200 and response.content == PDF
        assert response.headers["content-type"] == "application/pdf"
    blobs.delete(ref)
    assert await provider.open() is None  # a dangling blob reads as no resume


async def test_resume_and_assistant_need_public_read(pdf: Path) -> None:
    async for client, _, _, keys in _client("public", FileResume(pdf)):
        leads = (await issue_key(keys, "leads", ["leads:write"]))[1]
        assert (await client.get("/v1/resume")).status_code == 401
        assert (await client.get("/v1/assistant")).status_code == 401
        assert (await client.get("/v1/resume", headers={"X-API-Key": leads})).status_code == 403
        assert (await client.get("/v1/assistant", headers={"X-API-Key": leads})).status_code == 403


async def test_assistant_info_discloses_the_ai_twin() -> None:
    async for client, key, _, _ in _client("public", None, public_owner_name="Kavya Testpal"):
        body = result(await client.get("/v1/assistant", headers={"X-API-Key": key}))
        assert set(body) == {"greeting", "disclosure", "privacy", "availability"}
        assert "Kavya Testpal's AI twin" in body["greeting"]
        assert "AI twin, not Kavya Testpal in person" in body["disclosure"]
        assert "90 days" in body["privacy"] and "hash" in body["privacy"]
        assert body["availability"] == "open to backend roles"
    async for client, key, _, _ in _client("public", None):
        body = result(await client.get("/v1/assistant", headers={"X-API-Key": key}))
        assert body["greeting"].startswith("Hi, I'm an AI twin.")


def test_provider_follows_the_profile(tmp_path: Path) -> None:
    blobs = BlobStore(tmp_path, Fernet.generate_key())
    store = MemoryProfileStore()
    vault = Settings(_env_file=None, deployment_profile="vault")
    public = Settings(_env_file=None, deployment_profile="public", public_resume_path=tmp_path)
    assert isinstance(resume_provider(vault, store, blobs), VaultResume)
    assert isinstance(resume_provider(public, store, blobs), FileResume)
