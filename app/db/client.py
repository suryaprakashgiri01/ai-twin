"""MongoDB client lifecycle: one `AsyncMongoClient` per process, Beanie on top of it.

Startup fails fast: a wrong password or an unreachable server stops the app with one clear log
line, rather than surfacing later as a failed request.
"""

from __future__ import annotations

from collections.abc import Sequence

import structlog
from beanie import Document, init_beanie
from pymongo import AsyncMongoClient
from pymongo.errors import OperationFailure, PyMongoError, ServerSelectionTimeoutError

from app.core.config import SERVICE_NAME, Settings

logger = structlog.get_logger(__name__)

MongoClient = AsyncMongoClient[dict[str, object]]

# MongoDB's server error code for a failed SCRAM handshake.
_AUTH_FAILED = 18


class StorageStartupError(RuntimeError):
    """A store could not be opened at startup; the message says which and why."""


def create_client(settings: Settings) -> MongoClient:
    return AsyncMongoClient(
        settings.mongodb_uri,
        appname=SERVICE_NAME,
        tz_aware=True,
        serverSelectionTimeoutMS=settings.mongodb_timeout_ms,
    )


async def verify_connection(client: MongoClient) -> None:
    """Ping with credentials, turning driver errors into one actionable startup error."""
    try:
        await client.admin.command("ping")
    except OperationFailure as exc:
        if exc.code == _AUTH_FAILED:
            logger.error(
                "mongodb authentication failed; check the MONGODB_URI credentials in .env",
                error_code=exc.code,
            )
            raise StorageStartupError("MongoDB authentication failed") from exc
        raise
    except ServerSelectionTimeoutError as exc:
        logger.error("mongodb unreachable; is the stack up? (make status)")
        raise StorageStartupError("MongoDB unreachable") from exc
    except PyMongoError as exc:  # pragma: no cover - other driver failures, same treatment
        logger.error("mongodb connection failed", error_type=type(exc).__name__)
        raise StorageStartupError("MongoDB connection failed") from exc


async def init_mongo(
    settings: Settings, models: Sequence[type[Document]], client: MongoClient | None = None
) -> MongoClient:
    """Connect, verify, and initialise Beanie, which creates any missing declared index."""
    client = client or create_client(settings)
    try:
        await verify_connection(client)
        await init_beanie(database=client[settings.mongodb_database], document_models=list(models))
    except BaseException:
        await client.close()
        raise
    logger.info("mongodb ready", database=settings.mongodb_database, models=len(models))
    return client
