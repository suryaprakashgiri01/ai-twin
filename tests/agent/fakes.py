"""Fakes for owner-agent tests: a scripted embedder, a document filter and a seeded Qdrant."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from qdrant_client import AsyncQdrantClient

from app.kb.embedding import Embedding
from app.kb.retrieval.retriever import SearchFilters
from app.kb.vectors import QdrantIndex, VectorPoint, collection_for, point_id
from app.vector.qdrant import ensure_collections

DIM = 8


def axis(i: int, weight: float = 1.0) -> list[float]:
    """A unit-ish dense vector along one axis: chunks on different axes do not match."""
    vector = [0.0] * DIM
    vector[i % DIM] = weight
    return vector


@dataclass
class MapEmbedder:
    """Returns the embedding registered for a text; unknown texts get a far-away vector."""

    vectors: dict[str, Embedding] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)

    def register(self, text: str, dense: list[float], sparse: dict[int, float]) -> None:
        self.vectors[text] = Embedding(
            dense=dense, sparse_indices=list(sparse), sparse_values=list(sparse.values())
        )

    async def embed(self, texts: list[str]) -> list[Embedding]:
        self.calls += texts
        fallback = Embedding(dense=axis(DIM - 1), sparse_indices=[999], sparse_values=[1.0])
        return [self.vectors.get(t, fallback) for t in texts]


@dataclass
class FakeDocs:
    """Live-document check and tag/date matching without MongoDB."""

    deleted: set[str] = field(default_factory=set)
    matches: list[str] | None = None
    seen_filters: list[SearchFilters] = field(default_factory=list)

    async def live(self, document_ids: Sequence[str]) -> set[str]:
        return {d for d in document_ids if d not in self.deleted}

    async def matching(self, filters: SearchFilters) -> list[str]:
        self.seen_filters.append(filters)
        return list(self.matches or [])


class SpyQdrant:
    """Wraps a client and records every collection a query touched."""

    def __init__(self, client: AsyncQdrantClient) -> None:
        self._client = client
        self.queried: list[str] = []

    async def query_points(self, collection_name: str, **kwargs: Any) -> Any:
        self.queried.append(collection_name)
        return await self._client.query_points(collection_name=collection_name, **kwargs)


def chunk_point(
    document_id: str,
    ordinal: int,
    *,
    tier: str,
    text: str,
    dense: list[float],
    sparse: dict[int, float],
    canary: bool = False,
    category: str = "notes",
    title: str = "Note",
) -> VectorPoint:
    return VectorPoint(
        id=point_id(document_id, ordinal),
        dense=dense,
        sparse_indices=list(sparse),
        sparse_values=list(sparse.values()),
        payload={
            "document_id": document_id,
            "tier": tier,
            "category": category,
            "published": tier == "public",
            "canary": canary,
            "ordinal": ordinal,
            "title": title,
            "text": text,
        },
    )


async def memory_qdrant() -> AsyncQdrantClient:
    client = AsyncQdrantClient(location=":memory:")
    await ensure_collections(client, "vault", DIM)
    return client


async def seed(client: AsyncQdrantClient, *points: VectorPoint) -> None:
    index = QdrantIndex(client)
    for p in points:
        tier = p.payload["tier"]
        collection = "kb_public" if tier == "public" else collection_for(tier)
        await index.upsert(collection, [p])
