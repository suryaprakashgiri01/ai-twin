"""`query_records`: counts and lists of applications and interviews by aggregation.

The model never writes a query. It fills `RecordsQuery` (a closed schema) and this module turns
that into a fixed MongoDB pipeline, so a count is always exact and nothing can be modified.
"""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.db.documents import Record
from app.services.profile import RecordKind

GroupBy = Literal["company", "stage", "role", "month"]

# Items listed alongside a count; the count itself is never capped.
ITEM_LIMIT = 20

_GROUP_KEYS: dict[GroupBy, Any] = {
    "company": "$company",
    "stage": "$stage",
    "role": "$role",
    "month": {"$dateToString": {"format": "%Y-%m", "date": "$date"}},
}


class RecordsQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: RecordKind
    company: str | None = Field(default=None, max_length=200)
    stage: str | None = Field(default=None, max_length=100)
    date_from: date | None = None
    date_to: date | None = None
    group_by: GroupBy | None = None

    @model_validator(mode="after")
    def _ordered(self) -> RecordsQuery:
        if self.date_from and self.date_to and self.date_from > self.date_to:
            raise ValueError("date_from is after date_to")
        return self


class RecordGroup(BaseModel):
    key: str
    count: int


class RecordItem(BaseModel):
    company: str
    role: str
    stage: str
    date: date


class RecordsResult(BaseModel):
    query: RecordsQuery
    total: int
    groups: list[RecordGroup] = Field(default_factory=list)
    items: list[RecordItem] = Field(default_factory=list)

    def summary(self) -> str:
        """A plain-text answer built from the numbers alone; no model involved."""
        noun = self.query.kind if self.total == 1 else f"{self.query.kind}s"
        lines = [f"You have {self.total} {noun}{_scope(self.query)}."]
        if self.groups:
            lines += [f"- {g.key}: {g.count}" for g in self.groups]
        elif self.items:
            lines += [
                f"- {i.date.isoformat()} · {i.company} · {i.role} · {i.stage}" for i in self.items
            ]
            if self.total > len(self.items):
                lines.append(f"(latest {len(self.items)} of {self.total} shown)")
        return "\n".join(lines)


def _scope(query: RecordsQuery) -> str:
    parts = []
    if query.company:
        parts.append(f" at {query.company}")
    if query.stage:
        parts.append(f" in stage {query.stage}")
    if query.date_from and query.date_to:
        parts.append(f" from {query.date_from.isoformat()} to {query.date_to.isoformat()}")
    elif query.date_from:
        parts.append(f" since {query.date_from.isoformat()}")
    elif query.date_to:
        parts.append(f" up to {query.date_to.isoformat()}")
    return "".join(parts)


def _exact(value: str) -> dict[str, str]:
    return {"$regex": f"^{re.escape(value.strip())}$", "$options": "i"}


def match_stage(query: RecordsQuery) -> dict[str, Any]:
    match: dict[str, Any] = {"kind": query.kind}
    if query.company:
        match["company"] = _exact(query.company)
    if query.stage:
        match["stage"] = _exact(query.stage)
    bounds: dict[str, datetime] = {}
    if query.date_from:
        bounds["$gte"] = datetime.combine(query.date_from, time.min)
    if query.date_to:
        # Inclusive of the whole last day.
        bounds["$lt"] = datetime.combine(query.date_to + timedelta(days=1), time.min)
    if bounds:
        match["date"] = bounds
    return match


def build_pipeline(query: RecordsQuery, item_limit: int = ITEM_LIMIT) -> list[dict[str, Any]]:
    facets: dict[str, Any] = {
        "total": [{"$count": "n"}],
        "items": [
            {"$sort": {"date": -1}},
            {"$limit": item_limit},
            {"$project": {"_id": 0, "company": 1, "role": 1, "stage": 1, "date": 1}},
        ],
    }
    if query.group_by:
        facets["groups"] = [
            {"$group": {"_id": _GROUP_KEYS[query.group_by], "count": {"$sum": 1}}},
            {"$sort": {"count": -1, "_id": 1}},
        ]
    return [{"$match": match_stage(query)}, {"$facet": facets}]


def parse_result(query: RecordsQuery, row: dict[str, Any]) -> RecordsResult:
    total = row.get("total") or [{"n": 0}]
    return RecordsResult(
        query=query,
        total=int(total[0]["n"]),
        groups=[
            RecordGroup(key=str(g["_id"]), count=int(g["count"])) for g in row.get("groups", [])
        ],
        items=[
            RecordItem(
                company=i["company"], role=i["role"], stage=i["stage"], date=i["date"].date()
            )
            for i in row.get("items", [])
        ],
    )


class RecordsTool(Protocol):
    async def query(self, query: RecordsQuery) -> RecordsResult: ...


class MongoRecordsTool:
    async def query(self, query: RecordsQuery) -> RecordsResult:
        cursor = await Record.get_pymongo_collection().aggregate(build_pipeline(query))
        rows = await cursor.to_list()
        return parse_result(query, rows[0] if rows else {})
