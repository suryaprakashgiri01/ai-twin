"""Give leads stored before the opportunities feature the new fields' defaults.

Only leads without a `status` are touched, and each is marked `legacy`, so running it again
changes nothing and `down` removes exactly what `up` added. A legacy lead counts as already
handled: it is `reviewed`, never emailed again, and expires LEAD_RETENTION_DAYS (default 365)
after it was created.
"""

from __future__ import annotations

from typing import Any

from pymongo.asynchronous.database import AsyncDatabase

DESCRIPTION = "set opportunity defaults on existing leads"
RETENTION_MS = 365 * 86_400_000
BACKFILLED = (
    "status",
    "source",
    "details",
    "duplicates",
    "attempts",
    "updated_at",
    "expires_at",
    "legacy",
)


async def up(db: AsyncDatabase[dict[str, Any]]) -> None:
    await db["leads"].update_many(
        {"status": {"$exists": False}},
        [
            {
                "$set": {
                    "status": "reviewed",
                    "source": "form",
                    "details": {},
                    "duplicates": 0,
                    "attempts": 0,
                    "updated_at": "$created_at",
                    "expires_at": {"$add": ["$created_at", RETENTION_MS]},
                    "legacy": True,
                }
            }
        ],
    )


async def down(db: AsyncDatabase[dict[str, Any]]) -> None:
    await db["leads"].update_many({"legacy": True}, {"$unset": dict.fromkeys(BACKFILLED, "")})
