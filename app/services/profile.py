"""The owner's profile singleton and structured records (applications, interviews).

Records are plain data for the owner's own tracking, not RAG content: they are never chunked,
embedded or published.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Protocol

from beanie import PydanticObjectId
from pydantic import BaseModel, ConfigDict, Field

from app.db.documents import PROFILE_ID, BlobRef, Profile, Record, utcnow

RecordKind = Literal["application", "interview"]


class ProfileIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    availability: str = Field(default="not looking", max_length=200)
    notice_period: str | None = Field(default=None, max_length=100)
    preferred_roles: list[str] = Field(default_factory=list, max_length=20)
    locations: list[str] = Field(default_factory=list, max_length=20)
    reply_time: str | None = Field(default=None, max_length=100)
    resume_document_id: str | None = None


class ProfileOut(BaseModel):
    availability: str
    notice_period: str | None
    preferred_roles: list[str]
    locations: list[str]
    reply_time: str | None = None
    resume_document_id: str | None
    resume_blob: BlobRef | None
    updated_at: datetime


class RecordIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: RecordKind
    company: str = Field(min_length=1, max_length=200)
    role: str = Field(min_length=1, max_length=200)
    stage: str = Field(min_length=1, max_length=100)
    date: datetime
    notes: str | None = Field(default=None, max_length=10_000)


class RecordPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    company: str | None = Field(default=None, min_length=1, max_length=200)
    role: str | None = Field(default=None, min_length=1, max_length=200)
    stage: str | None = Field(default=None, min_length=1, max_length=100)
    date: datetime | None = None
    notes: str | None = Field(default=None, max_length=10_000)


class RecordOut(RecordIn):
    model_config = ConfigDict(extra="ignore")

    id: str


class ProfileStore(Protocol):
    async def get(self) -> ProfileOut: ...

    async def put(self, body: ProfileIn, resume_blob: BlobRef | None) -> ProfileOut: ...


class RecordStore(Protocol):
    async def list(self, kind: RecordKind | None) -> list[RecordOut]: ...

    async def create(self, body: RecordIn) -> RecordOut: ...

    async def update(
        self, record_id: PydanticObjectId, changes: dict[str, Any]
    ) -> RecordOut | None: ...

    async def delete(self, record_id: PydanticObjectId) -> bool: ...


def _profile_out(doc: Profile) -> ProfileOut:
    return ProfileOut(
        availability=doc.availability,
        notice_period=doc.notice_period,
        preferred_roles=doc.preferred_roles,
        locations=doc.locations,
        reply_time=doc.reply_time,
        resume_document_id=str(doc.resume_document_id) if doc.resume_document_id else None,
        resume_blob=doc.resume_blob,
        updated_at=doc.updated_at,
    )


def _record_out(doc: Record) -> RecordOut:
    return RecordOut(id=str(doc.id), **doc.model_dump(exclude={"id", "revision_id"}))


class MongoProfileStore:
    async def get(self) -> ProfileOut:
        doc = await Profile.get(PROFILE_ID) or Profile()
        return _profile_out(doc)

    async def put(self, body: ProfileIn, resume_blob: BlobRef | None) -> ProfileOut:
        resume = PydanticObjectId(body.resume_document_id) if body.resume_document_id else None
        doc = Profile(
            **body.model_dump(exclude={"resume_document_id"}),
            resume_document_id=resume,
            resume_blob=resume_blob,
            updated_at=utcnow(),
        )
        await doc.save()
        return _profile_out(doc)


class MongoRecordStore:
    async def list(self, kind: RecordKind | None) -> list[RecordOut]:
        query = Record.find({"kind": kind} if kind else {}).sort("-date")
        return [_record_out(d) for d in await query.to_list()]

    async def create(self, body: RecordIn) -> RecordOut:
        doc = Record(**body.model_dump())
        await doc.insert()
        return _record_out(doc)

    async def update(
        self, record_id: PydanticObjectId, changes: dict[str, Any]
    ) -> RecordOut | None:
        doc = await Record.get(record_id)
        if doc is None:
            return None
        for key, value in changes.items():
            setattr(doc, key, value)
        await doc.save()
        return _record_out(doc)

    async def delete(self, record_id: PydanticObjectId) -> bool:
        doc = await Record.get(record_id)
        if doc is None:
            return False
        await doc.delete()
        return True
