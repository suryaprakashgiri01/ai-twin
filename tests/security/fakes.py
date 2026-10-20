"""An in-memory event store for the alert worker, and a clock tests move by hand."""

from __future__ import annotations

import itertools
from collections.abc import Collection, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta

from app.db.documents import new_uid
from app.security.alerts import StoredEvent

T0 = datetime.fromisoformat("2026-10-19T04:00:00+00:00")  # 09:30 in Asia/Kolkata
_ids = itertools.count(1)


@dataclass
class MemoryEventStore:
    events: dict[str, StoredEvent] = field(default_factory=dict)

    def add(
        self,
        kind: str,
        at: datetime,
        *,
        severity: str = "medium",
        rules: Sequence[str] = (),
        visitor_hash: str | None = None,
        surface: str | None = None,
    ) -> StoredEvent:
        event = StoredEvent(
            id=f"{next(_ids):024x}",
            kind=kind,
            severity=severity,
            rules=tuple(rules),
            visitor_hash=visitor_hash,
            surface=surface,
            created_at=at,
            uid=new_uid(),
        )
        self.events[event.id] = event
        return event

    async def pending(self, kinds: Collection[str], since: datetime) -> list[StoredEvent]:
        rows = [
            e
            for e in self.events.values()
            if e.kind in kinds and e.alerted_at is None and e.created_at >= since
        ]
        return sorted(rows, key=lambda e: e.created_at)

    async def mark_alerted(self, ids: Sequence[str], at: datetime) -> None:
        for i in ids:
            self.events[i] = replace(self.events[i], alerted_at=at)

    async def undigested(self, since: datetime, before: datetime) -> list[StoredEvent]:
        rows = [
            e
            for e in self.events.values()
            if e.digested_at is None and since <= e.created_at < before
        ]
        return sorted(rows, key=lambda e: e.created_at)

    async def mark_digested(self, ids: Sequence[str], at: datetime) -> None:
        for i in ids:
            self.events[i] = replace(self.events[i], digested_at=at)

    async def recent(
        self,
        *,
        kind: str | None = None,
        severity: str | None = None,
        since: datetime | None = None,
        limit: int = 50,
    ) -> list[StoredEvent]:
        rows = [
            e
            for e in self.events.values()
            if (kind is None or e.kind == kind)
            and (severity is None or e.severity == severity)
            and (since is None or e.created_at >= since)
        ]
        return sorted(rows, key=lambda e: e.created_at, reverse=True)[:limit]


def minutes(n: float) -> timedelta:
    return timedelta(minutes=n)
