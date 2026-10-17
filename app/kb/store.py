"""Persistence for ingestion and review, behind a protocol so stages can be tested in memory.

Views are the document fields plus `id`; the MongoDB store maps them to Beanie documents.
"""

from __future__ import annotations

from typing import Any, Protocol

from beanie import PydanticObjectId
from pymongo.errors import DuplicateKeyError

from app.db.documents import (
    TERMINAL_STATUSES,
    Canary,
    Chunk,
    ChunkFields,
    KbDocument,
    KbDocumentFields,
    Source,
    SourceFields,
    utcnow,
)


class SourceView(SourceFields):
    id: PydanticObjectId


class DocumentView(KbDocumentFields):
    id: PydanticObjectId


REVIEW_STATUSES = ("pending", "quarantined")


class KbStore(Protocol):
    async def create_source(self, fields: SourceFields) -> tuple[SourceView, bool]:
        """The new source and True, or the existing one with the same checksum and False."""
        ...

    async def get_source(self, source_id: PydanticObjectId) -> SourceView | None: ...

    async def update_source(self, source_id: PydanticObjectId, **changes: Any) -> None: ...

    async def unfinished_sources(self) -> list[SourceView]: ...

    async def create_document(self, fields: KbDocumentFields) -> DocumentView: ...

    async def get_document(self, document_id: PydanticObjectId) -> DocumentView | None: ...

    async def update_document(self, document_id: PydanticObjectId, **changes: Any) -> None: ...

    async def documents_for_source(self, source_id: PydanticObjectId) -> list[DocumentView]: ...

    async def review_queue(self, limit: int) -> list[DocumentView]: ...

    async def purge_document(self, document_id: PydanticObjectId) -> None:
        """Hard delete a document with its chunks and canaries (an unfinished job's output)."""
        ...

    async def save_chunks(self, chunks: list[ChunkFields]) -> None: ...

    async def chunks_for(self, document_id: PydanticObjectId) -> list[ChunkFields]: ...

    async def update_chunks(self, document_id: PydanticObjectId, **changes: Any) -> None: ...

    async def delete_chunks(self, document_id: PydanticObjectId) -> None: ...

    async def add_canary(self, document_id: PydanticObjectId, marker: str) -> None: ...

    async def delete_canaries(self, document_id: PydanticObjectId) -> None: ...


def _source_view(doc: Source) -> SourceView:
    assert doc.id is not None
    return SourceView(id=doc.id, **doc.model_dump(include=set(SourceFields.model_fields)))


def _document_view(doc: KbDocument) -> DocumentView:
    assert doc.id is not None
    return DocumentView(id=doc.id, **doc.model_dump(include=set(KbDocumentFields.model_fields)))


def _chunk_fields(doc: Chunk) -> ChunkFields:
    return ChunkFields(**doc.model_dump(include=set(ChunkFields.model_fields)))


class MongoKbStore:
    async def create_source(self, fields: SourceFields) -> tuple[SourceView, bool]:
        existing = await Source.find_one({"kind": fields.kind, "checksum": fields.checksum})
        if existing is not None:
            return _source_view(existing), False
        doc = Source(**fields.model_dump())
        try:
            await doc.insert()
        except DuplicateKeyError:  # a concurrent submit of the same bytes won the race
            raced = await Source.find_one({"kind": fields.kind, "checksum": fields.checksum})
            assert raced is not None
            return _source_view(raced), False
        return _source_view(doc), True

    async def get_source(self, source_id: PydanticObjectId) -> SourceView | None:
        doc = await Source.get(source_id)
        return _source_view(doc) if doc else None

    async def update_source(self, source_id: PydanticObjectId, **changes: Any) -> None:
        await Source.find_one({"_id": source_id}).update(
            {"$set": {**changes, "updated_at": utcnow()}}
        )

    async def unfinished_sources(self) -> list[SourceView]:
        docs = await Source.find({"status": {"$nin": sorted(TERMINAL_STATUSES)}}).to_list()
        return [_source_view(d) for d in docs]

    async def create_document(self, fields: KbDocumentFields) -> DocumentView:
        doc = KbDocument(**fields.model_dump())
        await doc.insert()
        return _document_view(doc)

    async def get_document(self, document_id: PydanticObjectId) -> DocumentView | None:
        doc = await KbDocument.get(document_id)
        return _document_view(doc) if doc else None

    async def update_document(self, document_id: PydanticObjectId, **changes: Any) -> None:
        await KbDocument.find_one({"_id": document_id}).update(
            {"$set": {**changes, "updated_at": utcnow()}}
        )

    async def documents_for_source(self, source_id: PydanticObjectId) -> list[DocumentView]:
        docs = await KbDocument.find({"source_id": source_id}).to_list()
        return [_document_view(d) for d in docs]

    async def review_queue(self, limit: int) -> list[DocumentView]:
        docs = (
            await KbDocument.find(
                {"review_status": {"$in": list(REVIEW_STATUSES)}, "deleted_at": None}
            )
            .sort("-created_at")
            .limit(limit)
            .to_list()
        )
        return [_document_view(d) for d in docs]

    async def purge_document(self, document_id: PydanticObjectId) -> None:
        await self.delete_chunks(document_id)
        await self.delete_canaries(document_id)
        await KbDocument.find_one({"_id": document_id}).delete()

    async def save_chunks(self, chunks: list[ChunkFields]) -> None:
        if chunks:
            await Chunk.insert_many([Chunk(**c.model_dump()) for c in chunks])

    async def chunks_for(self, document_id: PydanticObjectId) -> list[ChunkFields]:
        docs = await Chunk.find({"document_id": document_id}).sort("+ordinal").to_list()
        return [_chunk_fields(d) for d in docs]

    async def update_chunks(self, document_id: PydanticObjectId, **changes: Any) -> None:
        await Chunk.find({"document_id": document_id}).update({"$set": changes})

    async def delete_chunks(self, document_id: PydanticObjectId) -> None:
        await Chunk.find({"document_id": document_id}).delete()

    async def add_canary(self, document_id: PydanticObjectId, marker: str) -> None:
        await Canary(document_id=document_id, marker=marker).insert()

    async def delete_canaries(self, document_id: PydanticObjectId) -> None:
        await Canary.find({"document_id": document_id}).delete()
