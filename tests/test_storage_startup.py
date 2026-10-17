from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from cryptography.fernet import Fernet
from pymongo.errors import OperationFailure, ServerSelectionTimeoutError

from app.cache.redis import open_redis
from app.core.app_factory import create_app
from app.core.config import Settings
from app.db import client as db_client
from app.db.client import StorageStartupError, verify_connection
from app.db.migrations import discover
from app.vector.qdrant import open_qdrant
from scripts import _common, setup
from tests.conftest import FakeCheck

ROOT = Path(__file__).resolve().parents[1]


class FakeAdmin:
    def __init__(self, error: Exception | None) -> None:
        self.error = error

    async def command(self, name: str) -> dict[str, Any]:
        if self.error:
            raise self.error
        return {"ok": 1}


class FakeMongo:
    def __init__(self, error: Exception | None = None) -> None:
        self.admin = FakeAdmin(error)
        self.closed = False

    async def close(self) -> None:
        self.closed = True


async def test_auth_failure_fails_fast_with_a_clear_line(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from app.core.logging import setup_logging

    setup_logging(level="INFO", log_format="json")
    with pytest.raises(StorageStartupError, match="authentication failed"):
        await verify_connection(FakeMongo(OperationFailure("auth", code=18)))  # type: ignore[arg-type]
    assert "mongodb authentication failed" in capsys.readouterr().out


async def test_unreachable_and_other_failures() -> None:
    with pytest.raises(StorageStartupError, match="unreachable"):
        await verify_connection(FakeMongo(ServerSelectionTimeoutError("x")))  # type: ignore[arg-type]
    with pytest.raises(OperationFailure):
        await verify_connection(FakeMongo(OperationFailure("other", code=13)))  # type: ignore[arg-type]
    await verify_connection(FakeMongo())  # type: ignore[arg-type]


async def test_init_mongo_closes_the_client_on_failure() -> None:
    fake = FakeMongo(OperationFailure("auth", code=18))
    settings = Settings(_env_file=None)
    with pytest.raises(StorageStartupError):
        await db_client.init_mongo(settings, [], client=fake)  # type: ignore[arg-type]
    assert fake.closed


async def test_redis_and_qdrant_unreachable_fail_startup() -> None:
    settings = Settings(
        _env_file=None, redis_url="redis://127.0.0.1:1/0", qdrant_url="http://127.0.0.1:1"
    )
    with pytest.raises(StorageStartupError, match="Redis"):
        await open_redis(settings)
    with pytest.raises(StorageStartupError, match="Qdrant"):
        await open_qdrant(settings)


async def test_lifespan_opens_resources_and_exposes_them() -> None:
    opened: list[str] = []

    @asynccontextmanager
    async def factory(_settings: Settings) -> AsyncIterator[Any]:
        opened.append("open")
        yield "resources"
        opened.append("close")

    app = create_app(
        Settings(_env_file=None, app_env="test", log_format="json"),
        health_checks=[FakeCheck("mongodb")],
        resources_factory=factory,
    )
    async with app.router.lifespan_context(app):
        assert app.state.resources == "resources"
    assert opened == ["open", "close"]


def test_migrations_are_discovered_in_version_order() -> None:
    migrations = discover()
    assert [m.version for m in migrations][:2] == ["0001", "0002"]
    assert migrations == sorted(migrations, key=lambda m: m.version)


def test_fresh_env_gets_a_valid_fernet_key_and_salt() -> None:
    rendered = setup.render_env((ROOT / ".env.example").read_text(encoding="utf-8"))
    values = dict(
        line.split("=", 1)
        for line in rendered.splitlines()
        if "=" in line and not line.startswith("#")
    )
    Fernet(values["BLOB_ENCRYPTION_KEY"])
    assert values["VISITOR_HASH_SALT"] not in ("", "CHANGE_ME")
    assert values["EMBEDDING_DIM"] == "384"


def test_existing_env_gets_missing_keys_appended_values_untouched(tmp_path: Path) -> None:
    example = tmp_path / ".env.example"
    example.write_text(
        "A=1\nSECRET=CHANGE_ME\nBLOB_ENCRYPTION_KEY=CHANGE_ME\nNEW=default\n", encoding="utf-8"
    )
    env = tmp_path / ".env"
    env.write_text("A=kept\nSECRET=mine", encoding="utf-8")  # no trailing newline

    assert setup.append_missing(env, example) == ["BLOB_ENCRYPTION_KEY", "NEW"]
    text = env.read_text(encoding="utf-8")
    assert text.startswith("A=kept\nSECRET=mine\n")
    values = _common.read_env(env)
    assert values["A"] == "kept" and values["SECRET"] == "mine" and values["NEW"] == "default"
    Fernet(values["BLOB_ENCRYPTION_KEY"])

    assert setup.append_missing(env, example) == []
    assert env.read_text(encoding="utf-8") == text
    assert setup.ensure_env(env, example) is False
