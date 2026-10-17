"""Process-wide storage handles, opened in the app lifespan and closed on shutdown."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass

from app.core.config import Settings
from app.db.client import MongoClient, init_mongo
from app.db.documents import DOCUMENT_MODELS
from app.db.migrations import run_migrations


@dataclass
class Resources:
    mongo: MongoClient


ResourcesFactory = Callable[[Settings], AbstractAsyncContextManager[Resources]]


@asynccontextmanager
async def open_resources(settings: Settings) -> AsyncIterator[Resources]:
    mongo = await init_mongo(settings, DOCUMENT_MODELS)
    try:
        await run_migrations(mongo[settings.mongodb_database])
        yield Resources(mongo=mongo)
    finally:
        await mongo.close()
