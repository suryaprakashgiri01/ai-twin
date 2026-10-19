"""Readiness checks for the service's dependencies.

Each check is a small object with a name and an async ``ping``. They run concurrently, each
under its own timeout, so one slow dependency cannot stall the probe.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Literal, Protocol

import httpx
import structlog
from pymongo import AsyncMongoClient
from redis.asyncio import Redis

from app.core.config import Settings

logger = structlog.get_logger(__name__)

CheckStatus = Literal["ok", "unavailable"]


class DependencyCheck(Protocol):
    name: str

    async def ping(self) -> None: ...


class MongoCheck:
    name = "mongodb"

    def __init__(self, uri: str, timeout_seconds: float) -> None:
        self._uri = uri
        self._timeout_ms = int(timeout_seconds * 1000)

    async def ping(self) -> None:
        client: AsyncMongoClient[dict[str, object]] = AsyncMongoClient(
            self._uri, serverSelectionTimeoutMS=self._timeout_ms
        )
        try:
            await client.admin.command("ping")
        finally:
            await client.close()


class RedisCheck:
    name = "redis"

    def __init__(self, url: str, timeout_seconds: float) -> None:
        self._url = url
        self._timeout = timeout_seconds

    async def ping(self) -> None:
        client = Redis.from_url(self._url, socket_connect_timeout=self._timeout)
        try:
            await client.ping()
        finally:
            await client.aclose()


class QdrantCheck:
    name = "qdrant"

    def __init__(self, url: str, timeout_seconds: float) -> None:
        self._url = url.rstrip("/")
        self._timeout = timeout_seconds

    async def ping(self) -> None:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.get(f"{self._url}/readyz")
            response.raise_for_status()


def default_checks(settings: Settings) -> list[DependencyCheck]:
    timeout = settings.health_check_timeout_seconds
    return [
        MongoCheck(settings.mongodb_uri, timeout),
        RedisCheck(settings.redis_url, timeout),
        QdrantCheck(settings.qdrant_url, timeout),
    ]


async def _run_one(check: DependencyCheck, timeout_seconds: float) -> CheckStatus:
    try:
        await asyncio.wait_for(check.ping(), timeout=timeout_seconds)
    except Exception as exc:
        logger.warning("readiness check failed", check=check.name, error=type(exc).__name__)
        return "unavailable"
    return "ok"


async def run_checks(
    checks: Sequence[DependencyCheck], timeout_seconds: float
) -> dict[str, CheckStatus]:
    # The outer timeout is a backstop: client timeouts should fire first.
    results = await asyncio.gather(*(_run_one(c, timeout_seconds + 0.5) for c in checks))
    return {check.name: status for check, status in zip(checks, results, strict=True)}
