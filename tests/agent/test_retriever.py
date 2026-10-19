"""Owner-agent T1: hybrid retrieval over tier-scoped collections (AC1, AC2)."""

from __future__ import annotations

from typing import Any

import pytest

from app.kb.retrieval.retriever import (
    COLLECTION_TIERS,
    Retriever,
    SearchFilters,
    collections_for,
)
from llm_orchestrator import Tier
from tests.agent.fakes import (
    FakeDocs,
    MapEmbedder,
    SpyQdrant,
    axis,
    chunk_point,
    memory_qdrant,
    seed,
)

QUESTION = "what did I learn about consistent hashing"


@pytest.mark.parametrize(
    ("tier", "expected"),
    [
        (Tier.PUBLIC, ["kb_public"]),
        (Tier.PRIVATE, ["kb_public", "kb_private"]),
        (Tier.CONFIDENTIAL, ["kb_public", "kb_private", "kb_confidential"]),
        (Tier.RESTRICTED, ["kb_public", "kb_private", "kb_confidential"]),
    ],
)
def test_collections_follow_max_tier(tier: Tier, expected: list[str]) -> None:
    assert collections_for(tier) == expected


async def harness(**docs: Any) -> tuple[Retriever, SpyQdrant, MapEmbedder, FakeDocs]:
    client = await memory_qdrant()
    embedder = MapEmbedder()
    embedder.register(QUESTION, axis(0), {7: 1.0})
    await seed(
        client,
        chunk_point("pub", 0, tier="public", text="public talk", dense=axis(0), sparse={1: 1}),
        chunk_point("priv", 0, tier="private", text="private note", dense=axis(0), sparse={7: 1}),
        chunk_point("conf", 0, tier="confidential", text="offer", dense=axis(0), sparse={7: 1}),
        chunk_point(
            "conf", 1, tier="confidential", text="CANARY", dense=axis(0), sparse={7: 1}, canary=True
        ),
        chunk_point(
            "priv", 1, tier="private", text="CANARY-P", dense=axis(0), sparse={7: 1}, canary=True
        ),
    )
    spy = SpyQdrant(client)
    fake_docs = FakeDocs(**docs)
    retriever = Retriever(spy, embedder, fake_docs)  # type: ignore[arg-type]
    return retriever, spy, embedder, fake_docs


async def test_private_caller_never_queries_confidential() -> None:
    retriever, spy, _, _ = await harness()
    hits = await retriever.search(QUESTION, max_tier=Tier.PRIVATE)
    assert "kb_confidential" not in spy.queried
    assert set(spy.queried) == {"kb_public", "kb_private"}
    assert {h.document_id for h in hits} == {"pub", "priv"}
    assert all(h.tier <= Tier.PRIVATE for h in hits)


async def test_public_caller_queries_kb_public_only() -> None:
    retriever, spy, _, _ = await harness()
    hits = await retriever.search(QUESTION, max_tier=Tier.PUBLIC)
    assert spy.queried == ["kb_public"]
    assert [h.document_id for h in hits] == ["pub"]


async def test_canary_and_deleted_chunks_never_returned() -> None:
    retriever, _, _, _ = await harness(deleted={"priv"})
    hits = await retriever.search(QUESTION, max_tier=Tier.CONFIDENTIAL)
    texts = {h.text for h in hits}
    assert "CANARY" not in texts and "CANARY-P" not in texts
    assert {h.document_id for h in hits} == {"pub", "conf"}
    assert {h.tier for h in hits} == {Tier.PUBLIC, Tier.CONFIDENTIAL}


async def test_rrf_returns_sparse_only_and_dense_only_matches() -> None:
    client = await memory_qdrant()
    embedder = MapEmbedder()
    # The query's exact term is sparse index 42; its meaning points along axis 3.
    embedder.register("raft leader election", axis(3), {42: 1.0})
    await seed(
        client,
        chunk_point("exact", 0, tier="private", text="raft term", dense=axis(5), sparse={42: 1}),
        chunk_point("para", 0, tier="private", text="consensus", dense=axis(3), sparse={8: 1}),
        chunk_point("other", 0, tier="private", text="cooking", dense=axis(6), sparse={9: 1}),
    )
    retriever = Retriever(client, embedder, FakeDocs(), prefetch_limit=1)
    hits = await retriever.search("raft leader election", max_tier=Tier.PRIVATE)
    assert {h.document_id for h in hits} == {"exact", "para"}


async def test_published_chunk_is_returned_once() -> None:
    client = await memory_qdrant()
    embedder = MapEmbedder()
    embedder.register(QUESTION, axis(0), {7: 1.0})
    copy = chunk_point("doc", 0, tier="public", text="talk", dense=axis(0), sparse={7: 1})
    await seed(client, copy)
    from app.kb.vectors import QdrantIndex

    await QdrantIndex(client).upsert("kb_private", [copy])
    hits = await Retriever(client, embedder, FakeDocs()).search(QUESTION, max_tier=Tier.PRIVATE)
    assert [(h.document_id, h.ordinal) for h in hits] == [("doc", 0)]


async def test_category_filter_and_document_filters() -> None:
    client = await memory_qdrant()
    embedder = MapEmbedder()
    embedder.register(QUESTION, axis(0), {7: 1.0})
    await seed(
        client,
        chunk_point("a", 0, tier="private", text="a", dense=axis(0), sparse={7: 1}, category="x"),
        chunk_point("b", 0, tier="private", text="b", dense=axis(0), sparse={7: 1}, category="y"),
    )
    docs = FakeDocs(matches=["b"])
    retriever = Retriever(client, embedder, docs)
    by_category = await retriever.search(
        QUESTION, max_tier=Tier.PRIVATE, filters=SearchFilters(category="x")
    )
    assert [h.document_id for h in by_category] == ["a"]
    assert docs.seen_filters == []  # category alone needs no document lookup
    by_tag = await retriever.search(
        QUESTION, max_tier=Tier.PRIVATE, filters=SearchFilters(tags=["backend"])
    )
    assert [h.document_id for h in by_tag] == ["b"]
    docs.matches = []
    none = await retriever.search(
        QUESTION, max_tier=Tier.PRIVATE, filters=SearchFilters(tags=["nothing"])
    )
    assert none == []


def test_collection_map_covers_every_tier_below_restricted() -> None:
    assert sorted(COLLECTION_TIERS.values()) == [Tier.PUBLIC, Tier.PRIVATE, Tier.CONFIDENTIAL]
