from __future__ import annotations

from collections.abc import AsyncIterator, Sequence

import httpx
import pytest
from fastapi import FastAPI

from app.core.app_factory import create_app
from app.core.config import Settings
from app.services.health import DependencyCheck


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
