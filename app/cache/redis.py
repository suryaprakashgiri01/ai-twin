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


# Bumped on every publish and unpublish, so cached public answers from before go stale.
KB_PUBLIC_VERSION_KEY = "kb_public_version"


def answer_cache_key(version: int, question_hash: str) -> str:
    """One guarded public answer, for a normalised question at one `kb_public_version`."""
    return f"ac:{version}:{question_hash}"


def resume_downloads_key(day: str) -> str:
    """Resume downloads on one UTC day (`YYYYMMDD`)."""
    return f"resume:downloads:{day}"


def resume_visitors_key(day: str) -> str:
    """HyperLogLog of the visitor hashes that downloaded the resume on one UTC day."""
    return f"resume:visitors:{day}"


def security_burst_key(scope: str) -> str:
    """Set once per window so a burst of the same security event is recorded once."""
    return f"sec:burst:{scope}"


def lead_visitor_key(visitor_hash: str) -> str:
    """Leads one visitor left in the day since their first; expires a day after it."""
    return f"lead:v:{visitor_hash}:day"


def lead_global_key(day: str) -> str:
    """Leads from every visitor on one UTC day (`YYYYMMDD`)."""
    return f"lead:global:{day}"


def lead_digest_key(day: str) -> str:
    """Set when the suppressed-leads digest for one local day (`YYYYMMDD`) has gone out."""
    return f"lead:digest:{day}"


def alert_cooldown_key(policy: str) -> str:
    """Set (with the cooldown as its TTL) when an alert for `policy` has gone out."""
    return f"alert:cooldown:{policy}"


def alert_last_key(policy: str) -> str:
    """ISO time of the last alert email for `policy`: events after it are a follow-up's count."""
    return f"alert:last:{policy}"


def alert_digest_key(day: str) -> str:
    """Set when the security digest for one local day (`YYYYMMDD`) has been handled."""
    return f"alert:digest:{day}"


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
