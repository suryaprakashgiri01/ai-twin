"""Narrow fused hits to the best `top_k` with a local cross-encoder, dropping weak ones.

The model (`Xenova/ms-marco-MiniLM-L-6-v2`, ~80 MB) downloads once into `vault/models/fastembed`
and runs in a worker thread. Its raw scores are logits; a sigmoid maps them onto 0..1 so the
threshold reads as a probability-like cut-off.
"""

from __future__ import annotations

import asyncio
import math
import threading
from pathlib import Path
from typing import Any, Protocol

import structlog

from app.kb.retrieval.retriever import Hit

logger = structlog.get_logger(__name__)

RERANK_MODEL = "Xenova/ms-marco-MiniLM-L-6-v2"


class Reranker(Protocol):
    async def rerank(self, query: str, hits: list[Hit], top_k: int) -> list[Hit]: ...


def _sigmoid(logit: float) -> float:
    return 1.0 / (1.0 + math.exp(-logit))


def select_top(hits: list[Hit], scores: list[float], top_k: int, min_score: float) -> list[Hit]:
    """Hits re-scored, best first, at most `top_k`, none under `min_score`."""
    scored = [
        h.model_copy(update={"score": s})
        for h, s in zip(hits, scores, strict=True)
        if s >= min_score
    ]
    scored.sort(key=lambda h: h.score, reverse=True)
    return scored[:top_k]


class FusedOrderReranker:
    """Keeps the retriever's fused order: for tests and when no cross-encoder is wanted."""

    async def rerank(self, query: str, hits: list[Hit], top_k: int) -> list[Hit]:
        return hits[:top_k]


class CrossEncoderReranker:
    def __init__(self, cache_dir: Path, *, min_score: float = 0.01, batch_size: int = 32) -> None:
        self._cache_dir = cache_dir
        self._min_score = min_score
        self._batch_size = batch_size
        self._lock = threading.Lock()
        self._model: Any = None

    def _load(self) -> Any:
        with self._lock:
            if self._model is None:
                from fastembed.rerank.cross_encoder import TextCrossEncoder

                self._cache_dir.mkdir(parents=True, exist_ok=True)
                self._model = TextCrossEncoder(RERANK_MODEL, cache_dir=str(self._cache_dir))
                logger.info("rerank model loaded", model=RERANK_MODEL)
            return self._model

    def _score_sync(self, query: str, texts: list[str]) -> list[float]:
        model = self._load()
        logits = model.rerank(query, texts, batch_size=self._batch_size)
        return [_sigmoid(float(x)) for x in logits]

    async def rerank(self, query: str, hits: list[Hit], top_k: int) -> list[Hit]:
        if not hits:
            return []
        scores = await asyncio.to_thread(self._score_sync, query, [h.text for h in hits])
        kept = select_top(hits, scores, top_k, self._min_score)
        logger.info("kb rerank", candidates=len(hits), kept=len(kept), min_score=self._min_score)
        return kept
