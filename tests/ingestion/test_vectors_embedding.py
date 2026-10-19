"""Ingestion T7: the Qdrant index (qdrant-client local mode) and the fastembed wrapper (faked)."""

from __future__ import annotations

import sys
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from qdrant_client import AsyncQdrantClient

from app.kb.embedding import DENSE_DIM, FastEmbedder
from app.kb.vectors import QdrantIndex, VectorPoint, collection_for, point_id
from app.vector.qdrant import ensure_collections


@pytest.fixture
async def index() -> AsyncIterator[QdrantIndex]:
    client = AsyncQdrantClient(location=":memory:")
    await ensure_collections(client, "vault", DENSE_DIM)
    yield QdrantIndex(client)
    await client.close()


def point(document_id: str, ordinal: int) -> VectorPoint:
    return VectorPoint(
        id=point_id(document_id, ordinal),
        dense=[0.1 + ordinal / 100] * DENSE_DIM,
        sparse_indices=[3, 7],
        sparse_values=[0.5, 1.5],
        payload={"document_id": document_id, "ordinal": ordinal, "published": False},
    )


async def test_upsert_read_payload_count_delete(index: QdrantIndex) -> None:
    await index.upsert("kb_private", [point("d1", 0), point("d1", 1), point("d2", 0)])
    await index.upsert("kb_private", [])
    assert await index.count("kb_private") == 3
    assert await index.count("kb_private", "d1") == 2
    points = await index.points_for("kb_private", "d1")
    assert sorted(p.payload["ordinal"] for p in points) == [0, 1]
    assert len(points[0].dense) == DENSE_DIM
    assert points[0].sparse_indices == [3, 7]
    await index.set_payload("kb_private", "d1", {"published": True})
    assert {p.payload["published"] for p in await index.points_for("kb_private", "d1")} == {True}
    await index.delete_document("kb_private", "d1")
    assert await index.count("kb_private") == 1


def test_point_ids_are_deterministic_and_collections_follow_tier() -> None:
    assert point_id("d", 1) == point_id("d", 1) != point_id("d", 2)
    assert collection_for("public") == "kb_private"
    assert collection_for("private") == "kb_private"
    assert collection_for("confidential") == "kb_confidential"
    with pytest.raises(ValueError):
        collection_for("restricted")


class _Dense:
    def __init__(self, name: str, cache_dir: str) -> None:
        self.name, self.cache_dir = name, cache_dir

    def embed(self, texts: list[str], batch_size: int) -> Any:
        return [[0.5] * DENSE_DIM for _ in texts]


class _Sparse(_Dense):
    def embed(self, texts: list[str], batch_size: int) -> Any:
        return [SimpleNamespace(indices=[1, 9], values=[0.2, 0.8]) for _ in texts]


async def test_fastembedder_loads_once_and_embeds_both_kinds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = SimpleNamespace(TextEmbedding=_Dense, SparseTextEmbedding=_Sparse)
    monkeypatch.setitem(sys.modules, "fastembed", fake)
    embedder = FastEmbedder(tmp_path / "models")
    assert await embedder.embed([]) == []
    [first, second] = await embedder.embed(["a", "b"])
    assert len(first.dense) == DENSE_DIM
    assert second.sparse_indices == [1, 9]
    loaded = embedder._models
    await embedder.embed(["c"])
    assert embedder._models is loaded
    assert (tmp_path / "models").is_dir()
