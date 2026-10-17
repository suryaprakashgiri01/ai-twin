"""Owner review actions on documents: approve, publish, unpublish, relabel, release, delete.

Publishing copies a public, approved document's chunks (never canaries) from `kb_private` into
`kb_public`; the private copies stay for owner retrieval. A tier change moves the vectors to the
matching collection and resets approval, since approval was given at the old tier.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import structlog
from beanie import PydanticObjectId

from app.db.documents import ChunkFields, TierLabel, utcnow
from app.exceptions import AppError, ErrorCode
from app.kb import canaries
from app.kb.ingestion.pipeline import Pipeline
from app.kb.store import DocumentView, KbStore
from app.kb.vectors import PUBLIC_COLLECTION, VectorIndex, VectorPoint, collection_for, point_id

logger = structlog.get_logger(__name__)


def _conflict(message: str) -> AppError:
    return AppError(ErrorCode.CONFLICT, message)


@dataclass
class Publisher:
    store: KbStore
    vectors: VectorIndex
    pipeline: Pipeline

    async def get(self, document_id: PydanticObjectId) -> DocumentView:
        doc = await self.store.get_document(document_id)
        if doc is None or doc.deleted_at is not None:
            raise AppError(ErrorCode.NOT_FOUND, "Document not found")
        return doc

    async def approve(self, document_id: PydanticObjectId) -> DocumentView:
        doc = await self.get(document_id)
        if doc.review_status == "quarantined":
            raise _conflict("Release the quarantined document before approving it")
        await self.store.update_document(doc.id, review_status="approved", approved_at=utcnow())
        return await self.get(doc.id)

    async def publish(self, document_id: PydanticObjectId) -> tuple[DocumentView, int]:
        doc = await self.get(document_id)
        if doc.tier != "public":
            raise _conflict(f"Only public documents can be published (tier is {doc.tier})")
        if doc.review_status != "approved":
            raise _conflict("Approve the document before publishing it")
        source = collection_for(doc.tier)
        points = [
            p.model_copy(update={"payload": {**p.payload, "published": True, "tier": "public"}})
            for p in await self.vectors.points_for(source, str(doc.id))
            if not p.payload.get("canary")
        ]
        if not points:
            raise _conflict("The document has no indexed chunks to publish")
        await self.vectors.delete_document(PUBLIC_COLLECTION, str(doc.id))
        await self.vectors.upsert(PUBLIC_COLLECTION, points)
        await self.vectors.set_payload(source, str(doc.id), {"published": True})
        await self.store.update_chunks(doc.id, published=True)
        await self.store.update_document(doc.id, published=True, published_at=utcnow())
        return await self.get(doc.id), len(points)

    async def unpublish(self, document_id: PydanticObjectId) -> DocumentView:
        doc = await self.get(document_id)
        await self._withdraw(doc)
        return await self.get(doc.id)

    async def _withdraw(self, doc: DocumentView) -> None:
        await self.vectors.delete_document(PUBLIC_COLLECTION, str(doc.id))
        if doc.tier != "restricted":
            await self.vectors.set_payload(
                collection_for(doc.tier), str(doc.id), {"published": False}
            )
        await self.store.update_chunks(doc.id, published=False)
        await self.store.update_document(doc.id, published=False, published_at=None)

    async def update(self, document_id: PydanticObjectId, changes: dict[str, Any]) -> DocumentView:
        doc = await self.get(document_id)
        tier = changes.pop("tier", None)
        if changes:
            await self.store.update_document(doc.id, **changes)
            payload = {k: v for k, v in changes.items() if k in ("title", "category")}
            if payload and doc.tier != "restricted":
                for collection in {collection_for(doc.tier), PUBLIC_COLLECTION}:
                    await self.vectors.set_payload(collection, str(doc.id), payload)
        if tier is not None and tier != doc.tier:
            await self._relabel(await self.get(doc.id), tier)
        return await self.get(doc.id)

    async def _relabel(self, doc: DocumentView, tier: str) -> None:
        if doc.tier == "restricted":
            raise _conflict("A restricted document has no text to index; ingest it again")
        if doc.published:
            await self._withdraw(doc)
        label = TierLabel(tier=tier, reason="set by the owner", by="owner", confidence=1)
        changes: dict[str, Any] = {"tier": tier, "label": label.model_dump()}
        if doc.review_status == "approved":
            changes |= {"review_status": "pending", "approved_at": None}
        if doc.review_status != "quarantined":
            await self._move_vectors(doc, tier)
        if tier == "restricted":
            changes["text"] = None
        await self.store.update_document(doc.id, **changes)

    async def _move_vectors(self, doc: DocumentView, tier: str) -> None:
        old = collection_for(doc.tier)
        points = await self.vectors.points_for(old, str(doc.id))
        await self.vectors.delete_document(old, str(doc.id))
        if tier == "restricted":
            await self.store.delete_chunks(doc.id)
            await self.store.delete_canaries(doc.id)
            return
        if not canaries.needs_canary(tier):
            points = [p for p in points if not p.payload.get("canary")]
            await self.store.delete_canaries(doc.id)
        moved = [p.model_copy(update={"payload": {**p.payload, "tier": tier}}) for p in points]
        moved += await self._missing_canary(doc, tier, moved)
        await self.vectors.upsert(collection_for(tier), moved)
        await self.store.delete_chunks(doc.id)
        await self.store.save_chunks(
            [
                self._chunk(doc.id, p, tier)
                for p in sorted(moved, key=lambda p: int(p.payload.get("ordinal", 0)))
            ]
        )

    async def _missing_canary(
        self, doc: DocumentView, tier: str, points: list[VectorPoint]
    ) -> list[VectorPoint]:
        if not canaries.needs_canary(tier) or any(p.payload.get("canary") for p in points):
            return []
        ordinal = 1 + max((int(p.payload.get("ordinal", 0)) for p in points), default=-1)
        marker = canaries.new_marker()
        chunk = canaries.canary_chunk(ordinal, doc.title, marker)
        [embedding] = await self.pipeline.embedder.embed([chunk.text])
        await self.store.add_canary(doc.id, marker)
        return [
            VectorPoint(
                id=point_id(str(doc.id), ordinal),
                dense=embedding.dense,
                sparse_indices=embedding.sparse_indices,
                sparse_values=embedding.sparse_values,
                payload={
                    "document_id": str(doc.id),
                    "tier": tier,
                    "category": doc.category,
                    "published": False,
                    "canary": True,
                    "ordinal": ordinal,
                    "title": doc.title,
                    "text": chunk.text,
                },
            )
        ]

    @staticmethod
    def _chunk(document_id: PydanticObjectId, point: VectorPoint, tier: str) -> ChunkFields:
        return ChunkFields(
            document_id=document_id,
            ordinal=int(point.payload.get("ordinal", 0)),
            text=str(point.payload.get("text", "")),
            tier=tier,
            qdrant_point_id=point.id,
            canary=bool(point.payload.get("canary")),
        )

    async def release(self, document_id: PydanticObjectId) -> tuple[DocumentView, int]:
        """Owner says a quarantined document is fine: label and index it now."""
        doc = await self.get(document_id)
        if doc.review_status != "quarantined":
            raise _conflict("Only quarantined documents can be released")
        await self.store.update_document(doc.id, review_status="pending", quarantine_rules=[])
        indexed = await self.pipeline.relabel_and_index(await self.get(doc.id))
        return await self.get(doc.id), indexed

    async def delete(self, document_id: PydanticObjectId) -> None:
        doc = await self.get(document_id)
        await self.pipeline.delete_vectors(str(doc.id))
        await self.store.delete_chunks(doc.id)
        await self.store.update_document(
            doc.id, deleted_at=utcnow(), published=False, published_at=None
        )
