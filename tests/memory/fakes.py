"""In-memory memory store and a tiny embedder for owner-memory tests. Fake persona only."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, ClassVar

from pymongo.errors import DuplicateKeyError

from app.db.documents import MemoryStatus, OwnerMemoryFields, utcnow
from app.kb.embedding import Embedding
from app.services.memory import MemoryOut


class FakeMemoryStore:
    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}

    def _out(self, memory_id: str) -> MemoryOut:
        row = {k: v for k, v in self.rows[memory_id].items() if k not in ("norm", "uid")}
        return MemoryOut(id=memory_id, **row)

    async def by_status(self, statuses: Sequence[MemoryStatus]) -> list[MemoryOut]:
        found = [self._out(i) for i, r in self.rows.items() if r["status"] in statuses]
        return sorted(found, key=lambda m: m.updated_at, reverse=True)

    async def get(self, memory_id: str) -> MemoryOut | None:
        return self._out(memory_id) if memory_id in self.rows else None

    async def find_live(self, norm: str) -> MemoryOut | None:
        for memory_id, row in self.rows.items():
            if row["norm"] == norm and row["status"] != "archived":
                return self._out(memory_id)
        return None

    async def insert(self, fields: OwnerMemoryFields) -> MemoryOut:
        if fields.status != "archived" and await self.find_live(fields.norm):
            raise DuplicateKeyError("norm_live_unique")
        memory_id = fields.uid
        self.rows[memory_id] = fields.model_dump()
        return self._out(memory_id)

    async def update(self, memory_id: str, changes: dict[str, Any]) -> MemoryOut | None:
        if memory_id not in self.rows:
            return None
        self.rows[memory_id].update(changes, updated_at=utcnow())
        return self._out(memory_id)

    async def bump(self, memory_id: str, activate: bool) -> MemoryOut | None:
        row = self.rows.get(memory_id)
        if row is None:
            return None
        row["uses"] += 1
        row["updated_at"] = utcnow()
        if activate:
            row["status"] = "active"
        return self._out(memory_id)

    async def delete(self, memory_id: str) -> bool:
        return self.rows.pop(memory_id, None) is not None


class FakeAudit:
    def __init__(self) -> None:
        self.entries: list[tuple[str, str, str, dict[str, Any]]] = []

    async def record(self, actor: str, action: str, target: str, details: dict[str, Any]) -> None:
        self.entries.append((actor, action, target, details))


class WordEmbedder:
    """Bag-of-words vectors over a fixed vocabulary: near-identical sentences score ≥ 0.9."""

    VOCAB: ClassVar[tuple[str, ...]] = (
        "concise", "bullets", "sources", "first", "answers", "short", "dates", "oct", "show",
        "magnus", "eval", "project", "always", "use", "tables", "british", "spelling",
    )  # fmt: skip

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.calls = 0

    async def embed(self, texts: list[str]) -> list[Embedding]:
        self.calls += 1
        if self.fail:
            raise RuntimeError("model missing")
        out = []
        for text in texts:
            words = text.casefold().replace(",", " ").split()
            dense = [float(words.count(w)) for w in self.VOCAB]
            out.append(Embedding(dense=dense, sparse_indices=[], sparse_values=[]))
        return out
