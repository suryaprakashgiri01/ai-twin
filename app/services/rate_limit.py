"""Public message limits in Redis: per visitor (sliding minute and day) and a global daily cap.

A visitor is known only by the salted hash of their address. Each window is a sorted set of
message times, trimmed on every check; the global cap is one counter per UTC day. A refused
message is not counted, so a visitor who waits gets in again on time.
"""

from __future__ import annotations

import math
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from redis.asyncio import Redis

from app.cache.redis import rate_global_key, rate_visitor_key

Window = Literal["minute", "day", "global"]

MINUTE = 60
DAY = 86_400


@dataclass(frozen=True)
class Limits:
    per_minute: int = 8
    per_day: int = 60
    global_per_day: int = 1500


@dataclass(frozen=True)
class Decision:
    allowed: bool
    # Whole seconds until the refused window has room again; 0 when allowed.
    retry_after: int = 0
    window: Window | None = None


def _until_midnight(now: float) -> int:
    moment = datetime.fromtimestamp(now, UTC)
    midnight = (moment + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(1, math.ceil((midnight - moment).total_seconds()))


def _day(now: float) -> str:
    return datetime.fromtimestamp(now, UTC).strftime("%Y%m%d")


class RateLimiter:
    def __init__(
        self, redis: Redis, limits: Limits, *, clock: Callable[[], float] = time.time
    ) -> None:
        self._redis = redis
        self._limits = limits
        self._clock = clock

    async def hit(self, visitor_hash: str) -> Decision:
        """Count one message for `visitor_hash`, or refuse it with the time to wait."""
        now = self._clock()
        minute_key = rate_visitor_key(visitor_hash, "min")
        day_key = rate_visitor_key(visitor_hash, "day")
        global_key = rate_global_key(_day(now))
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.zremrangebyscore(minute_key, 0, now - MINUTE)
            pipe.zremrangebyscore(day_key, 0, now - DAY)
            pipe.zrange(minute_key, 0, 0, withscores=True)
            pipe.zcard(minute_key)
            pipe.zrange(day_key, 0, 0, withscores=True)
            pipe.zcard(day_key)
            pipe.get(global_key)
            _, _, minute_oldest, minute_count, day_oldest, day_count, total = await pipe.execute()
        refused = self._refusal(now, (minute_oldest, minute_count), (day_oldest, day_count), total)
        if refused is not None:
            return refused
        member = f"{now:.6f}:{uuid.uuid4().hex[:8]}"
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.zadd(minute_key, {member: now})
            pipe.expire(minute_key, MINUTE)
            pipe.zadd(day_key, {member: now})
            pipe.expire(day_key, DAY)
            pipe.incr(global_key)
            pipe.expire(global_key, 2 * DAY)
            await pipe.execute()
        return Decision(allowed=True)

    def _refusal(
        self,
        now: float,
        minute: tuple[list[tuple[str, float]], int],
        day: tuple[list[tuple[str, float]], int],
        total: str | None,
    ) -> Decision | None:
        limits = self._limits
        windows: tuple[tuple[Window, tuple[list[tuple[str, float]], int], int, int], ...] = (
            ("minute", minute, limits.per_minute, MINUTE),
            ("day", day, limits.per_day, DAY),
        )
        for window, (oldest, count), limit, span in windows:
            if count >= limit:
                started = oldest[0][1] if oldest else now
                wait = max(1, math.ceil(started + span - now))
                return Decision(allowed=False, retry_after=wait, window=window)
        if int(total or 0) >= limits.global_per_day:
            return Decision(allowed=False, retry_after=_until_midnight(now), window="global")
        return None
