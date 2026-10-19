"""Chunk vectors in Qdrant, one collection per tier, addressed by document.

`kb_private` holds private and not-yet-published public documents, `kb_confidential` the
confidential ones. `kb_public` only ever receives copies made by publishing.
"""

from __future__ import annotations

import uuid
from typing import Any, Protocol

from pydantic import BaseModel
from qdrant_client import AsyncQdrantClient, models

from app.vector.qdrant import DENSE, SPARSE

PUBLIC_COLLECTION = "kb_public"
TIER_COLLECTIONS = ("kb_private", "kb_confidential")
ALL_COLLECTIONS = (PUBLIC_COLLECTION, *TIER_COLLECTIONS)
_POINT_NAMESPACE = uuid.UUID("5b2f4c1e-8a7d-4e39-9c61-0d3f2a6b7e14")


def collection_for(tier: str) -> str:
    """Where a document's own vectors live. Restricted documents have none."""
    if tier == "restricted":
        raise ValueError("restricted documents are never embedded")
    return "kb_confidential" if tier == "confidential" else "kb_private"


def point_id(document_id: str, ordinal: int) -> str:
    """Deterministic, so re-running a job overwrites rather than duplicates."""
    return str(uuid.uuid5(_POINT_NAMESPACE, f"{document_id}:{ordinal}"))


class VectorPoint(BaseModel):
    id: str
    dense: list[float]
    sparse_indices: list[int]
    sparse_values: list[float]
    payload: dict[str, Any]


class VectorIndex(Protocol):
    async def upsert(self, collection: str, points: list[VectorPoint]) -> None: ...

    async def points_for(self, collection: str, document_id: str) -> list[VectorPoint]: ...

    async def delete_document(self, collection: str, document_id: str) -> None: ...

    async def set_payload(
        self, collection: str, document_id: str, payload: dict[str, Any]
    ) -> None: ...

    async def count(self, collection: str, document_id: str | None = None) -> int: ...


def _by_document(document_id: str) -> models.Filter:
    return models.Filter(
        must=[models.FieldCondition(key="document_id", match=models.MatchValue(value=document_id))]
    )


def _from_record(record: models.Record) -> VectorPoint:
    vectors = record.vector if isinstance(record.vector, dict) else {}
    dense = vectors.get(DENSE)
    sparse = vectors.get(SPARSE)
    return VectorPoint(
        id=str(record.id),
        dense=list(dense) if isinstance(dense, list) else [],
        sparse_indices=list(sparse.indices) if isinstance(sparse, models.SparseVector) else [],
        sparse_values=list(sparse.values) if isinstance(sparse, models.SparseVector) else [],
        payload=dict(record.payload or {}),
    )


class QdrantIndex:
    def __init__(self, client: AsyncQdrantClient) -> None:
        self._client = client

    async def upsert(self, collection: str, points: list[VectorPoint]) -> None:
        if not points:
            return
        await self._client.upsert(
            collection_name=collection,
            wait=True,
            points=[
                models.PointStruct(
                    id=p.id,
                    vector={
                        DENSE: p.dense,
                        SPARSE: models.SparseVector(
                            indices=p.sparse_indices, values=p.sparse_values
                        ),
                    },
                    payload=p.payload,
                )
                for p in points
            ],
        )

    async def points_for(self, collection: str, document_id: str) -> list[VectorPoint]:
        found: list[VectorPoint] = []
        offset: Any = None
        while True:
            records, offset = await self._client.scroll(
                collection_name=collection,
                scroll_filter=_by_document(document_id),
                with_vectors=True,
                with_payload=True,
                limit=256,
                offset=offset,
            )
            found += [_from_record(r) for r in records]
            if offset is None:
                return found

    async def delete_document(self, collection: str, document_id: str) -> None:
        await self._client.delete(
            collection_name=collection,
            points_selector=models.FilterSelector(filter=_by_document(document_id)),
            wait=True,
        )

    async def set_payload(self, collection: str, document_id: str, payload: dict[str, Any]) -> None:
        await self._client.set_payload(
            collection_name=collection, payload=payload, points=_by_document(document_id), wait=True
        )

    async def count(self, collection: str, document_id: str | None = None) -> int:
        result = await self._client.count(
            collection_name=collection,
            count_filter=_by_document(document_id) if document_id else None,
            exact=True,
        )
        return int(result.count)
