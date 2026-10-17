"""Owner agent against the compose stack: hybrid search on real Qdrant with real fastembed,
the records aggregation on MongoDB, the reranker, and history after a Redis flush.

Vectors go into throwaway `it_kb_*` collections, deleted at the end; MongoDB data goes into the
`ai_twin_it` database the shared fixture drops.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import date, datetime

import pytest
from beanie import PydanticObjectId
from qdrant_client import models

from app.agents.tools.records import MongoRecordsTool, RecordsQuery
from app.core.config import Settings
from app.core.resources import Resources
from app.db.documents import KbDocument, Record, utcnow
from app.kb.embedding import FastEmbedder
from app.kb.retrieval.rerank import CrossEncoderReranker
from app.kb.retrieval.retriever import Hit, MongoDocumentFilter, Retriever
from app.kb.vectors import QdrantIndex, VectorPoint, point_id
from app.services.conversations import ConversationRef, Turn
from app.vector.qdrant import DENSE, SPARSE
from llm_orchestrator import Tier
from tests.agent.fakes import SpyQdrant

pytestmark = pytest.mark.integration

SUFFIX = uuid.uuid4().hex[:6]
COLLECTIONS = {
    f"it_kb_public_{SUFFIX}": Tier.PUBLIC,
    f"it_kb_private_{SUFFIX}": Tier.PRIVATE,
    f"it_kb_confidential_{SUFFIX}": Tier.CONFIDENTIAL,
}
BY_TIER = {tier: name for name, tier in COLLECTIONS.items()}

# Fake-persona chunks. "Quorvex" is a made-up word: only the sparse (BM25) side can match it.
CHUNKS = {
    "exact": ("private", "Kavya named the scheduler service Quorvex during the hackathon."),
    "para": (
        "private",
        "Leader election lets a cluster of nodes agree on one coordinator after a failure.",
    ),
    "other": ("private", "A recipe for lemon rice with curry leaves and peanuts."),
    "conf": ("confidential", "The offer from Globex Labs included a joining bonus."),
    "deleted": ("private", "Old note about leader election in a cluster that was deleted."),
}


@pytest.fixture
def embedder(settings: Settings) -> FastEmbedder:
    return FastEmbedder(settings.models_dir / "fastembed")


@pytest.fixture
async def seeded(resources: Resources, embedder: FastEmbedder) -> AsyncIterator[dict[str, str]]:
    client = resources.qdrant
    for name in COLLECTIONS:
        await client.create_collection(
            collection_name=name,
            vectors_config={DENSE: models.VectorParams(size=384, distance=models.Distance.COSINE)},
            sparse_vectors_config={SPARSE: models.SparseVectorParams(modifier=models.Modifier.IDF)},
        )
    ids: dict[str, str] = {}
    for key, (tier, _) in CHUNKS.items():
        doc = KbDocument(
            source_id=PydanticObjectId(),
            title=f"Note {key}",
            category="notes",
            tier=tier,
            deleted_at=utcnow() if key == "deleted" else None,
        )
        await doc.insert()
        ids[key] = str(doc.id)
    texts = [text for _, text in CHUNKS.values()] + ["CANARY-IT Quorvex leader election"]
    vectors = await embedder.embed(texts)
    index = QdrantIndex(client)
    for (key, (tier, text)), emb in zip(CHUNKS.items(), vectors, strict=False):
        await index.upsert(BY_TIER[Tier.parse(tier)], [_point(ids[key], 0, tier, text, emb)])
    canary = _point(ids["exact"], 1, "private", texts[-1], vectors[-1], canary=True)
    await index.upsert(BY_TIER[Tier.PRIVATE], [canary])
    try:
        yield ids
    finally:
        for name in COLLECTIONS:
            await client.delete_collection(name)


def _point(
    document_id: str, ordinal: int, tier: str, text: str, emb: object, *, canary: bool = False
) -> VectorPoint:
    return VectorPoint(
        id=point_id(document_id, ordinal),
        dense=emb.dense,  # type: ignore[attr-defined]
        sparse_indices=emb.sparse_indices,  # type: ignore[attr-defined]
        sparse_values=emb.sparse_values,  # type: ignore[attr-defined]
        payload={
            "document_id": document_id,
            "tier": tier,
            "category": "notes",
            "published": False,
            "canary": canary,
            "ordinal": ordinal,
            "title": f"Note {ordinal}",
            "text": text,
        },
    )


def retriever(resources: Resources, embedder: FastEmbedder, client: object = None) -> Retriever:
    return Retriever(
        client or resources.qdrant,  # type: ignore[arg-type]
        embedder,
        MongoDocumentFilter(),
        collections=COLLECTIONS,
        prefetch_limit=2,
    )


async def test_rrf_finds_exact_term_and_paraphrase(
    resources: Resources, embedder: FastEmbedder, seeded: dict[str, str]
) -> None:
    search = retriever(resources, embedder)
    exact = await search.search("Quorvex", max_tier=Tier.PRIVATE, limit=3)
    assert exact[0].document_id == seeded["exact"]
    para = await search.search(
        "how do distributed nodes pick a coordinator?", max_tier=Tier.PRIVATE, limit=3
    )
    assert para[0].document_id == seeded["para"]


async def test_private_scope_canary_and_deleted_exclusion(
    resources: Resources, embedder: FastEmbedder, seeded: dict[str, str]
) -> None:
    spy = SpyQdrant(resources.qdrant)
    search = retriever(resources, embedder, spy)
    hits = await search.search(
        "Quorvex leader election joining bonus", max_tier=Tier.PRIVATE, limit=10
    )
    assert f"it_kb_confidential_{SUFFIX}" not in spy.queried
    found = {h.document_id for h in hits}
    assert seeded["conf"] not in found and seeded["deleted"] not in found
    assert all("CANARY-IT" not in h.text for h in hits)
    everything = await search.search("joining bonus offer", max_tier=Tier.CONFIDENTIAL, limit=10)
    assert seeded["conf"] in {h.document_id for h in everything}


async def test_reranker_puts_the_relevant_chunk_first(settings: Settings) -> None:
    reranker = CrossEncoderReranker(settings.models_dir / "fastembed", min_score=0.01)

    def hit(doc: str, text: str) -> Hit:
        return Hit(
            document_id=doc,
            ordinal=0,
            tier=Tier.PRIVATE,
            title=doc,
            category="notes",
            text=text,
            score=0.0,
            collection="kb_private",
        )

    kept = await reranker.rerank(
        "how do nodes elect a leader?",
        [hit("food", CHUNKS["other"][1]), hit("raft", CHUNKS["para"][1])],
        top_k=6,
    )
    assert kept[0].document_id == "raft"
    assert "food" not in {h.document_id for h in kept}


async def test_records_tool_counts_exactly(resources: Resources) -> None:
    rows = [
        ("interview", "Acme Widgets", datetime(2026, 9, 4)),
        ("interview", "Globex Labs", datetime(2026, 9, 18)),
        ("interview", "Initech Cloud", datetime(2026, 9, 30, 23, 0)),
        ("interview", "Acme Widgets", datetime(2026, 8, 21)),
        ("application", "Umbrella Data", datetime(2026, 9, 10)),
    ]
    for kind, company, day in rows:
        await Record(kind=kind, company=company, role="Backend", stage="screen", date=day).insert()
    tool = MongoRecordsTool()
    sept = await tool.query(
        RecordsQuery(kind="interview", date_from=date(2026, 9, 1), date_to=date(2026, 9, 30))
    )
    assert sept.total == 3 and len(sept.items) == 3
    assert sept.items[0].company == "Initech Cloud"  # newest first, last day included
    by_month = await tool.query(RecordsQuery(kind="interview", group_by="month"))
    assert {g.key: g.count for g in by_month.groups} == {"2026-09": 3, "2026-08": 1}
    acme = await tool.query(RecordsQuery(kind="interview", company="acme widgets"))
    assert acme.total == 2


async def test_owner_history_survives_a_redis_flush(resources: Resources) -> None:
    conversations = resources.conversations
    ref = await conversations.start("owner")
    await conversations.append_turn(ref, Turn(role="user", content="q"))
    await conversations.append_turn(
        ref,
        Turn(
            role="assistant",
            content="a [1]",
            route="kb_search",
            max_tier="private",
            citations=["d1"],
            withheld=1,
        ),
    )
    await conversations.drain()
    await resources.redis.flushdb()
    turns = await conversations.history(ref.id)
    assert [t.seq for t in turns] == [1, 2]
    assert turns[1].citations == ["d1"] and turns[1].withheld == 1
    assert turns[1].route == "kb_search" and turns[1].incomplete is False
    again = await conversations.append_turn(
        ConversationRef(ref.id, "owner"), Turn(role="user", content="q2")
    )
    assert again.seq == 3
