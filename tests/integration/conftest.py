"""Integration tests run against the compose stack (`make up`) with the `.env` it was made from.

They use their own MongoDB database and Redis db, dropped afterwards, so the vault data is left
alone. Qdrant collections are the real ones: their bootstrap is idempotent by design.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from app.core.config import Settings
from app.core.resources import Resources, open_resources

ENV_FILE = Path(__file__).resolve().parents[2] / ".env"
TEST_DATABASE = "ai_twin_it"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    if not ENV_FILE.exists():
        pytest.skip("integration tests need .env and the stack (make setup)")
    base = Settings(_env_file=ENV_FILE)
    redis_url = base.redis_url.rsplit("/", 1)[0] + "/15"
    return base.model_copy(
        update={
            "mongodb_database": TEST_DATABASE,
            "redis_url": redis_url,
            "blob_dir": tmp_path / "blobs",
            "log_format": "json",
        }
    )


@pytest.fixture
async def resources(settings: Settings) -> AsyncIterator[Resources]:
    async with open_resources(settings) as res:
        try:
            yield res
        finally:
            await res.redis.flushdb()
            await res.mongo.drop_database(TEST_DATABASE)
