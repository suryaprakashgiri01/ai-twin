"""Owner-agent T2: reranking, the context budget, citations and tier tracking (AC4, AC5)."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.kb.retrieval.context import build_context, estimate_tokens
from app.kb.retrieval.rerank import CrossEncoderReranker, FusedOrderReranker, select_top
from app.kb.retrieval.retriever import Hit
from llm_orchestrator import Tier


def hit(doc: str, ordinal: int = 0, *, tier: Tier = Tier.PRIVATE, text: str = "x" * 40) -> Hit:
    return Hit(
        document_id=doc,
        ordinal=ordinal,
        tier=tier,
        title=f"Title {doc}",
        category="notes",
        text=text,
        score=1.0,
        collection="kb_private",
    )


def test_select_top_orders_cuts_and_thresholds() -> None:
    hits = [hit("a"), hit("b"), hit("c"), hit("d")]
    kept = select_top(hits, [0.2, 0.9, 0.001, 0.5], top_k=2, min_score=0.01)
    assert [h.document_id for h in kept] == ["b", "d"]
    assert kept[0].score == 0.9


async def test_fused_order_reranker_keeps_order() -> None:
    hits = [hit("a"), hit("b"), hit("c")]
    assert [h.document_id for h in await FusedOrderReranker().rerank("q", hits, 2)] == ["a", "b"]


async def test_cross_encoder_scores_with_sigmoid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Model:
        def rerank(self, query: str, texts: list[str], batch_size: int) -> list[float]:
            return [4.0 if "raft" in t else -8.0 for t in texts]

    reranker = CrossEncoderReranker(tmp_path, min_score=0.01)
    monkeypatch.setattr(reranker, "_load", lambda: Model())
    kept = await reranker.rerank("q", [hit("a", text="cooking"), hit("b", text="raft")], 6)
    assert [h.document_id for h in kept] == ["b"]
    assert 0.98 < kept[0].score < 1.0
    assert await reranker.rerank("q", [], 6) == []


def test_citations_are_numbered_per_document() -> None:
    built = build_context(
        [hit("a", 0), hit("b", 0), hit("a", 1)], budget_tokens=1000, ceiling=Tier.CONFIDENTIAL
    )
    assert [c.citation for c in built.chunks] == [1, 2, 1]
    assert [(s.index, s.document_id) for s in built.sources] == [(1, "a"), (2, "b")]
    assert "[1] Title a" in built.render() and "[2] Title b" in built.render()


def test_tier_is_the_highest_tier_actually_used() -> None:
    hits = [hit("p", tier=Tier.PUBLIC), hit("c", tier=Tier.CONFIDENTIAL), hit("v")]
    built = build_context(hits, budget_tokens=1000, ceiling=Tier.CONFIDENTIAL)
    assert built.tier is Tier.CONFIDENTIAL and built.withheld == 0


def test_chunks_above_the_ceiling_are_withheld_and_counted() -> None:
    hits = [
        hit("c", 0, tier=Tier.CONFIDENTIAL),
        hit("c", 1, tier=Tier.CONFIDENTIAL),
        hit("d", tier=Tier.CONFIDENTIAL),
        hit("v"),
    ]
    built = build_context(hits, budget_tokens=1000, ceiling=Tier.PRIVATE)
    assert built.tier is Tier.PRIVATE
    assert built.withheld == 2  # documents, not chunks
    assert [s.document_id for s in built.sources] == ["v"]
    assert built.sources[0].index == 1


def test_budget_limits_context_but_keeps_first_chunk() -> None:
    big = "y" * 400  # ~100 tokens each
    built = build_context(
        [hit("a", text=big), hit("b", text=big), hit("c", text="z" * 8)],
        budget_tokens=120,
        ceiling=Tier.PRIVATE,
    )
    assert [c.hit.document_id for c in built.chunks] == ["a", "c"]
    assert built.tokens <= 120
    alone = build_context([hit("a", text=big * 10)], budget_tokens=50, ceiling=Tier.PRIVATE)
    assert len(alone.chunks) == 1


def test_empty_context() -> None:
    built = build_context([], budget_tokens=100, ceiling=Tier.CONFIDENTIAL)
    assert built.empty and built.tier is None and built.render() == ""
    assert estimate_tokens("") == 1
