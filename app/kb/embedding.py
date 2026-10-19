"""Local embeddings with fastembed: `BAAI/bge-small-en-v1.5` (dense, 384) and `Qdrant/bm25`.

Models download once into `vault/models/fastembed` (excluded from vault export) and load on
first use; inference runs in a worker thread so the event loop stays free.
"""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import structlog

logger = structlog.get_logger(__name__)

DENSE_MODEL = "BAAI/bge-small-en-v1.5"
SPARSE_MODEL = "Qdrant/bm25"
DENSE_DIM = 384


@dataclass(frozen=True)
class Embedding:
    dense: list[float]
    sparse_indices: list[int]
    sparse_values: list[float]


class Embedder(Protocol):
    async def embed(self, texts: list[str]) -> list[Embedding]: ...


class FastEmbedder:
    def __init__(self, cache_dir: Path, *, batch_size: int = 32) -> None:
        self._cache_dir = cache_dir
        self._batch_size = batch_size
        self._lock = threading.Lock()
        self._models: tuple[Any, Any] | None = None

    def _load(self) -> tuple[Any, Any]:
        with self._lock:
            if self._models is None:
                from fastembed import SparseTextEmbedding, TextEmbedding

                self._cache_dir.mkdir(parents=True, exist_ok=True)
                cache = str(self._cache_dir)
                self._models = (
                    TextEmbedding(DENSE_MODEL, cache_dir=cache),
                    SparseTextEmbedding(SPARSE_MODEL, cache_dir=cache),
                )
                logger.info("embedding models loaded", dense=DENSE_MODEL, sparse=SPARSE_MODEL)
            return self._models

    def _embed_sync(self, texts: list[str]) -> list[Embedding]:
        dense_model, sparse_model = self._load()
        dense = list(dense_model.embed(texts, batch_size=self._batch_size))
        sparse = list(sparse_model.embed(texts, batch_size=self._batch_size))
        return [
            Embedding(
                dense=[float(x) for x in d],
                sparse_indices=[int(i) for i in s.indices],
                sparse_values=[float(v) for v in s.values],
            )
            for d, s in zip(dense, sparse, strict=True)
        ]

    async def embed(self, texts: list[str]) -> list[Embedding]:
        if not texts:
            return []
        return await asyncio.to_thread(self._embed_sync, texts)
