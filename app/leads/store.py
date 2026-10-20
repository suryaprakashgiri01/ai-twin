"""Lead persistence behind a small protocol, so the service and outbox are testable in memory."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any, Protocol

from app.db.documents import Lead, LeadFields


class StoredLead(LeadFields):
    """`id` is the lead's public `uid`."""

    id: str


class LeadStore(Protocol):
    async def insert(self, fields: LeadFields) -> StoredLead: ...

    async def get(self, lead_id: str) -> StoredLead | None: ...

    async def update(self, lead_id: str, changes: Mapping[str, Any]) -> StoredLead | None: ...

    async def recent_duplicate(
        self, email_key: str, company_key: str, since: datetime
    ) -> StoredLead | None: ...

    async def merge_duplicate(self, lead_id: str, now: datetime) -> None: ...

    async def due(self, now: datetime, limit: int) -> list[StoredLead]: ...

    async def undigested_suppressed(self, before: datetime) -> list[StoredLead]: ...

    async def list(
        self, status: str | None, triage: str | None, limit: int
    ) -> list[StoredLead]: ...


def _stored(doc: Lead) -> StoredLead:
    return StoredLead(id=doc.uid, **doc.model_dump(exclude={"id", "revision_id"}))


def due_filter(now: datetime) -> dict[str, Any]:
    """A `new` lead not yet emailed whose next attempt time has come."""
    return {
        "status": "new",
        "emailed_at": None,
        "$or": [{"next_attempt_at": None}, {"next_attempt_at": {"$lte": now}}],
    }


class MongoLeadStore:
    async def insert(self, fields: LeadFields) -> StoredLead:
        doc = Lead(**fields.model_dump())
        await doc.insert()
        return _stored(doc)

    async def get(self, lead_id: str) -> StoredLead | None:
        doc = await Lead.find_one({"uid": lead_id})
        return _stored(doc) if doc is not None else None

    async def update(self, lead_id: str, changes: Mapping[str, Any]) -> StoredLead | None:
        await Lead.get_pymongo_collection().update_one({"uid": lead_id}, {"$set": dict(changes)})
        return await self.get(lead_id)

    async def recent_duplicate(
        self, email_key: str, company_key: str, since: datetime
    ) -> StoredLead | None:
        doc = await Lead.find_one(
            {"email_key": email_key, "company_key": company_key, "created_at": {"$gte": since}},
            sort=[("created_at", -1)],
        )
        return _stored(doc) if doc is not None else None

    async def merge_duplicate(self, lead_id: str, now: datetime) -> None:
        await Lead.get_pymongo_collection().update_one(
            {"uid": lead_id}, {"$inc": {"duplicates": 1}, "$set": {"updated_at": now}}
        )

    async def due(self, now: datetime, limit: int) -> list[StoredLead]:
        docs = await Lead.find(due_filter(now)).sort("created_at").limit(limit).to_list()
        return [_stored(d) for d in docs]

    async def undigested_suppressed(self, before: datetime) -> list[StoredLead]:
        query = {"status": "suppressed", "digested_at": None, "created_at": {"$lt": before}}
        return [_stored(d) for d in await Lead.find(query).sort("created_at").to_list()]

    async def list(self, status: str | None, triage: str | None, limit: int) -> list[StoredLead]:
        query: dict[str, Any] = {}
        if status:
            query["status"] = status
        if triage:
            query["triage"] = triage
        docs = await Lead.find(query).sort("-created_at").limit(limit).to_list()
        return [_stored(d) for d in docs]
