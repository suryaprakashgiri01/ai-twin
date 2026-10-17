"""Redis (Valkey) client lifecycle and key helpers. Keys are built here and nowhere else."""

from __future__ import annotations

import structlog
from redis.asyncio import Redis
from redis.exceptions import AuthenticationError, RedisError

from app.core.config import Settings
from app.db.client import StorageStartupError

logger = structlog.get_logger(__name__)


def conversation_key(conversation_id: str) -> str:
    """Capped list of the conversation's most recent turns, as JSON."""
    return f"conv:{conversation_id}"


def conversation_seq_key(conversation_id: str) -> str:
    """Counter that hands out each turn's `seq`; kept alive alongside the list."""
    return f"conv:{conversation_id}:seq"


def rate_visitor_key(visitor_hash: str, window: str) -> str:
    """Sorted set of one visitor's message times in a sliding window (`min` or `day`)."""
    return f"rl:v:{visitor_hash}:{window}"


def rate_global_key(day: str) -> str:
    """Counter of every public message on one UTC day (`YYYYMMDD`)."""
    return f"rl:global:{day}"


async def open_redis(settings: Settings) -> Redis:
    client: Redis = Redis.from_url(settings.redis_url, decode_responses=True)
    try:
        await client.ping()
    except AuthenticationError as exc:
        await client.aclose()
        logger.error("redis authentication failed; check REDIS_URL in .env")
        raise StorageStartupError("Redis authentication failed") from exc
    except RedisError as exc:
        await client.aclose()
        logger.error("redis unreachable; is the stack up? (make status)")
        raise StorageStartupError("Redis unreachable") from exc
    return client
