"""Hybrid search: dense + sparse prefetch fused with RRF, over the collections a tier allows.

`max_tier` decides which collections are queried at all, so a public caller can never reach
private vectors through a filter mistake. Canary chunks are excluded in the Qdrant filter and
again on the way out; deleted documents are dropped by a live-document check against MongoDB.
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta
from datetime import time as dtime
from typing import Any, Protocol

import structlog
from beanie import PydanticObjectId
from bson.errors import InvalidId
from pydantic import BaseModel, ConfigDict, Field
from qdrant_client import AsyncQdrantClient, models

from app.api.envelope import ApiModel
from app.db.documents import KbDocument
from app.kb.embedding import Embedder, Embedding
from app.vector.qdrant import DENSE, SPARSE
from llm_orchestrator import Tier

logger = structlog.get_logger(__name__)

# Each collection and the highest tier it can hold.
COLLECTION_TIERS: Mapping[str, Tier] = {
    "kb_public": Tier.PUBLIC,
    "kb_private": Tier.PRIVATE,
    "kb_confidential": Tier.CONFIDENTIAL,
}


def collections_for(max_tier: Tier, tiers: Mapping[str, Tier] = COLLECTION_TIERS) -> list[str]:
    """The collections a caller at `max_tier` may query. Restricted text is never embedded."""
    return [name for name, tier in tiers.items() if tier <= max_tier]


class SearchFilters(ApiModel):
    model_config = ConfigDict(extra="forbid")

    category: str | None = None
    tags: list[str] = Field(default_factory=list, max_length=20)
    date_from: date | None = None
    date_to: date | None = None

    def needs_documents(self) -> bool:
        """Tags and dates live on the document, not on the chunk payload."""
        return bool(self.tags) or self.date_from is not None or self.date_to is not None


class Hit(BaseModel):
    document_id: str
    ordinal: int
    tier: Tier
    title: str
    category: str
    text: str
    score: float
    collection: str
    # A document card (one summary per document) rather than a chunk of its text.
    card: bool = False

    @property
    def key(self) -> tuple[str, int]:
        return self.document_id, self.ordinal


class DocumentFilter(Protocol):
    async def live(self, document_ids: Sequence[str]) -> set[str]:
        """The ids among `document_ids` whose documents exist and are not deleted."""
        ...

    async def matching(self, filters: SearchFilters) -> list[str]:
        """Ids of live documents that match the tag and date filters."""
        ...


def _object_ids(ids: Sequence[str]) -> list[PydanticObjectId]:
    found = []
    for raw in ids:
        try:
            found.append(PydanticObjectId(raw))
        except (InvalidId, TypeError):
            continue
    return found


class MongoDocumentFilter:
    async def live(self, document_ids: Sequence[str]) -> set[str]:
        if not document_ids:
            return set()
        cursor = KbDocument.get_pymongo_collection().find(
            {"_id": {"$in": _object_ids(document_ids)}, "deleted_at": None}, {"_id": 1}
        )
        return {str(row["_id"]) async for row in cursor}

    async def matching(self, filters: SearchFilters) -> list[str]:
        query: dict[str, Any] = {"deleted_at": None}
        if filters.tags:
            query["tags"] = {"$all": filters.tags}
        bounds: dict[str, datetime] = {}
        if filters.date_from is not None:
            bounds["$gte"] = datetime.combine(filters.date_from, dtime.min)
        if filters.date_to is not None:
            bounds["$lt"] = datetime.combine(filters.date_to + timedelta(days=1), dtime.min)
        if bounds:
            query["occurred_at"] = bounds
        cursor = KbDocument.get_pymongo_collection().find(query, {"_id": 1})
        return [str(row["_id"]) async for row in cursor]


def _qdrant_filter(
    filters: SearchFilters | None, document_ids: list[str] | None, cards_only: bool = False
) -> models.Filter:
    must: list[models.Condition] = []
    if cards_only:
        must.append(models.FieldCondition(key="card", match=models.MatchValue(value=True)))
    if filters is not None and filters.category:
        must.append(
            models.FieldCondition(key="category", match=models.MatchValue(value=filters.category))
        )
    if document_ids is not None:
        must.append(
            models.FieldCondition(key="document_id", match=models.MatchAny(any=document_ids))
        )
    return models.Filter(
        must=must or None,
        must_not=[models.FieldCondition(key="canary", match=models.MatchValue(value=True))],
    )


def _hit(point: models.ScoredPoint, collection: str) -> Hit | None:
    payload = point.payload or {}
    if payload.get("canary") or "document_id" not in payload:
        return None
    try:
        tier = Tier.parse(payload.get("tier", "private"))
    except ValueError:
        return None
    return Hit(
        document_id=str(payload["document_id"]),
        ordinal=int(payload.get("ordinal", 0)),
        tier=tier,
        title=str(payload.get("title", "")),
        category=str(payload.get("category", "")),
        text=str(payload.get("text", "")),
        score=float(point.score),
        collection=collection,
        card=bool(payload.get("card")),
    )


class Retriever:
    def __init__(
        self,
        client: AsyncQdrantClient,
        embedder: Embedder,
        documents: DocumentFilter,
        *,
        collections: Mapping[str, Tier] = COLLECTION_TIERS,
        prefetch_limit: int = 30,
    ) -> None:
        self._client = client
        self._embedder = embedder
        self._documents = documents
        self._collections = collections
        self._prefetch = prefetch_limit

    async def search(
        self,
        query: str,
        *,
        max_tier: Tier,
        filters: SearchFilters | None = None,
        limit: int = 20,
        cards_only: bool = False,
        document_ids: Sequence[str] | None = None,
    ) -> list[Hit]:
        """Fused hits across the allowed collections, best first, one per chunk.

        `cards_only` keeps document cards; `document_ids` limits the search to those documents.
        """
        started = time.perf_counter()
        names = collections_for(max_tier, self._collections)
        ids = list(document_ids) if document_ids is not None else None
        if filters is not None and filters.needs_documents():
            matching = await self._documents.matching(filters)
            ids = matching if ids is None else [i for i in ids if i in set(matching)]
        if ids is not None and not ids:
            self._log(names, 0, 0, started, max_tier)
            return []
        embedding = (await self._embedder.embed([query]))[0]
        qfilter = _qdrant_filter(filters, ids, cards_only)
        raw: list[Hit] = []
        for name in names:
            raw += await self._query(name, embedding, qfilter, limit)
        hits = await self._dedupe_live(raw, limit)
        self._log(names, len(raw), len(hits), started, max_tier)
        return hits

    async def _query(
        self, collection: str, embedding: Embedding, qfilter: models.Filter, limit: int
    ) -> list[Hit]:
        sparse = models.SparseVector(
            indices=embedding.sparse_indices, values=embedding.sparse_values
        )
        response = await self._client.query_points(
            collection_name=collection,
            prefetch=[
                models.Prefetch(
                    query=embedding.dense, using=DENSE, limit=self._prefetch, filter=qfilter
                ),
                models.Prefetch(query=sparse, using=SPARSE, limit=self._prefetch, filter=qfilter),
            ],
            query=models.FusionQuery(fusion=models.Fusion.RRF),
            query_filter=qfilter,
            limit=limit,
            with_payload=True,
        )
        return [h for p in response.points if (h := _hit(p, collection)) is not None]

    async def _dedupe_live(self, raw: list[Hit], limit: int) -> list[Hit]:
        # A published chunk sits in kb_public and kb_private: keep its best-scoring copy.
        best: dict[tuple[str, int], Hit] = {}
        for hit in sorted(raw, key=lambda h: h.score, reverse=True):
            best.setdefault(hit.key, hit)
        live = await self._documents.live(sorted({h.document_id for h in best.values()}))
        return [h for h in best.values() if h.document_id in live][:limit]

    @staticmethod
    def _log(names: list[str], raw: int, kept: int, started: float, max_tier: Tier) -> None:
        logger.info(
            "kb search",
            max_tier=max_tier.label,
            collections=names,
            raw_hits=raw,
            hits=kept,
            latency_ms=round((time.perf_counter() - started) * 1000, 1),
        )
