"""In-memory KbStore, VectorIndex and embedder for unit tests of the ingestion pipeline."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from typing import Any

from beanie import PydanticObjectId

from app.db.documents import (
    TERMINAL_STATUSES,
    ChunkFields,
    KbDocumentFields,
    SourceFields,
    new_uid,
    utcnow,
)
from app.kb.embedding import DENSE_DIM, Embedding
from app.kb.store import REVIEW_STATUSES, DocumentView, SourceView
from app.kb.vectors import VectorPoint


class MemoryKbStore:
    def __init__(self) -> None:
        self.sources: dict[PydanticObjectId, SourceView] = {}
        self.documents: dict[PydanticObjectId, DocumentView] = {}
        self.chunks: list[ChunkFields] = []
        self.canaries: dict[str, PydanticObjectId] = {}

    async def create_source(self, fields: SourceFields) -> tuple[SourceView, bool]:
        for source in self.sources.values():
            if source.kind == fields.kind and source.checksum == fields.checksum:
                return source, False
        view = SourceView(id=PydanticObjectId(), **fields.model_dump())
        self.sources[view.id] = view
        return view, True

    async def get_source(self, source_id: PydanticObjectId) -> SourceView | None:
        return self.sources.get(source_id)

    async def source_by_uid(self, uid: str) -> SourceView | None:
        return next((s for s in self.sources.values() if s.uid == uid), None)

    async def update_source(self, source_id: PydanticObjectId, **changes: Any) -> None:
        current = self.sources[source_id]
        merged = {**current.model_dump(), **changes, "updated_at": utcnow()}
        self.sources[source_id] = SourceView.model_validate(merged)

    async def unfinished_sources(self) -> list[SourceView]:
        return [s for s in self.sources.values() if s.status not in TERMINAL_STATUSES]

    async def create_document(self, fields: KbDocumentFields) -> DocumentView:
        view = DocumentView(id=PydanticObjectId(), **fields.model_dump())
        self.documents[view.id] = view
        return view

    async def get_document(self, document_id: PydanticObjectId) -> DocumentView | None:
        return self.documents.get(document_id)

    async def document_by_uid(self, uid: str) -> DocumentView | None:
        return next((d for d in self.documents.values() if d.uid == uid), None)

    async def update_document(self, document_id: PydanticObjectId, **changes: Any) -> None:
        current = self.documents[document_id]
        merged = {**current.model_dump(), **changes, "updated_at": utcnow()}
        self.documents[document_id] = DocumentView.model_validate(merged)

    async def documents_for_source(self, source_id: PydanticObjectId) -> list[DocumentView]:
        return [d for d in self.documents.values() if d.source_id == source_id]

    async def review_queue(self, limit: int) -> list[DocumentView]:
        docs = [
            d
            for d in self.documents.values()
            if d.review_status in REVIEW_STATUSES and d.deleted_at is None
        ]
        return sorted(docs, key=lambda d: d.created_at, reverse=True)[:limit]

    async def find_by_title(self, text: str, limit: int) -> list[DocumentView]:
        words = set(text.casefold().split())
        docs = [
            d
            for d in self.documents.values()
            if d.deleted_at is None and words & set(d.title.casefold().split())
        ]
        return docs[:limit]

    async def purge_document(self, document_id: PydanticObjectId) -> None:
        await self.delete_chunks(document_id)
        await self.delete_canaries(document_id)
        self.documents.pop(document_id, None)

    async def save_chunks(self, chunks: list[ChunkFields]) -> None:
        for chunk in chunks:
            if any(
                c.document_id == chunk.document_id and c.ordinal == chunk.ordinal
                for c in self.chunks
            ):
                raise ValueError("duplicate chunk")  # mirrors the unique index
        self.chunks += chunks

    async def chunks_for(self, document_id: PydanticObjectId) -> list[ChunkFields]:
        return sorted(
            (c for c in self.chunks if c.document_id == document_id), key=lambda c: c.ordinal
        )

    async def update_chunks(self, document_id: PydanticObjectId, **changes: Any) -> None:
        self.chunks = [
            c.model_copy(update=changes) if c.document_id == document_id else c for c in self.chunks
        ]

    async def delete_chunks(self, document_id: PydanticObjectId) -> None:
        self.chunks = [c for c in self.chunks if c.document_id != document_id]

    async def add_canary(self, document_id: PydanticObjectId, marker: str) -> None:
        assert marker not in self.canaries
        self.canaries[marker] = document_id

    async def delete_canaries(self, document_id: PydanticObjectId) -> None:
        self.canaries = {m: d for m, d in self.canaries.items() if d != document_id}


class MemoryIndex:
    def __init__(self) -> None:
        self.collections: dict[str, dict[str, VectorPoint]] = {
            "kb_public": {},
            "kb_private": {},
            "kb_confidential": {},
        }

    async def upsert(self, collection: str, points: list[VectorPoint]) -> None:
        for point in points:
            self.collections[collection][point.id] = point

    async def points_for(self, collection: str, document_id: str) -> list[VectorPoint]:
        return [
            p
            for p in self.collections[collection].values()
            if p.payload.get("document_id") == document_id
        ]

    async def delete_document(self, collection: str, document_id: str) -> None:
        self.collections[collection] = {
            k: p
            for k, p in self.collections[collection].items()
            if p.payload.get("document_id") != document_id
        }

    async def set_payload(self, collection: str, document_id: str, payload: dict[str, Any]) -> None:
        for point in await self.points_for(collection, document_id):
            point.payload.update(payload)

    async def count(self, collection: str, document_id: str | None = None) -> int:
        if document_id is None:
            return len(self.collections[collection])
        return len(await self.points_for(collection, document_id))

    def all_payload_text(self) -> str:
        return "\n".join(
            str(p.payload) for points in self.collections.values() for p in points.values()
        )


class FakeEmbedder:
    def __init__(self) -> None:
        self.texts: list[str] = []

    async def embed(self, texts: list[str]) -> list[Embedding]:
        self.texts += texts
        result = []
        for text in texts:
            digest = hashlib.sha256(text.encode()).digest()
            dense = [digest[i % len(digest)] / 255 for i in range(DENSE_DIM)]
            result.append(Embedding(dense=dense, sparse_indices=[1, 2], sparse_values=[0.5, 0.5]))
        return result


class MemoryProfileStore:
    def __init__(self) -> None:
        from app.services.profile import ProfileOut

        self.profile = ProfileOut(
            availability="not looking",
            notice_period=None,
            preferred_roles=[],
            locations=[],
            resume_document_id=None,
            resume_blob=None,
            updated_at=utcnow(),
        )

    async def get(self) -> Any:
        return self.profile

    async def put(self, body: Any, resume: Any) -> Any:
        self.profile = self.profile.model_copy(
            update={
                **body.model_dump(),
                "resume_document_id": body.resume_document_id if resume else None,
                "resume_blob": resume[1] if resume else None,
                "updated_at": utcnow(),
            }
        )
        return self.profile


class MemoryRecordStore:
    def __init__(self) -> None:
        self.rows: dict[str, Any] = {}

    async def list(self, kind: str | None) -> list[Any]:
        rows = [r for r in self.rows.values() if kind is None or r.kind == kind]
        return sorted(rows, key=lambda r: r.date, reverse=True)

    async def create(self, body: Any) -> Any:
        from app.services.profile import RecordOut

        record = RecordOut(id=new_uid(), **body.model_dump())
        self.rows[record.id] = record
        return record

    async def create_many(self, bodies: Sequence[Any]) -> Sequence[Any]:
        return [await self.create(body) for body in bodies]

    async def update(self, record_id: str, changes: dict[str, Any]) -> Any:
        record = self.rows.get(record_id)
        if record is None:
            return None
        self.rows[record.id] = record.model_copy(update=changes)
        return self.rows[record.id]

    async def delete(self, record_id: str) -> bool:
        return self.rows.pop(record_id, None) is not None
