from __future__ import annotations

from collections.abc import AsyncIterator, Iterator, Sequence
from typing import Any
from unittest import mock

import httpx
import pytest
import structlog
from fastapi import FastAPI

from app.core.app_factory import create_app
from app.core.config import Settings
from app.services.health import DependencyCheck


@pytest.fixture(autouse=True, scope="session")
def uncached_loggers() -> Iterator[None]:
    """`create_app` caches loggers on first use, and a logger cached under one test's app config
    ignores a later test's `capture_logs`, so test order could hide log lines. No caching here."""
    configure = structlog.configure

    def uncached(**kwargs: Any) -> None:
        configure(**{**kwargs, "cache_logger_on_first_use": False})

    with mock.patch.object(structlog, "configure", uncached):
        yield


class FakeCheck:
    def __init__(self, name: str, *, fail: bool = False) -> None:
        self.name = name
        self.fail = fail

    async def ping(self) -> None:
        if self.fail:
            raise ConnectionError(self.name)


def make_app(checks: Sequence[DependencyCheck] | None = None) -> FastAPI:
    settings = Settings(_env_file=None, app_env="test", log_format="json")
    return create_app(settings, health_checks=checks or [FakeCheck("mongodb")])


@pytest.fixture
def app() -> FastAPI:
    return make_app()


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
