from __future__ import annotations

import pytest
from qdrant_client import models

from app.db.client import StorageStartupError
from app.vector.qdrant import COLLECTIONS, PAYLOAD_INDEXES, ensure_collections
from tests.fakes import FakeQdrant


async def test_vault_profile_creates_every_tier_with_hybrid_vectors() -> None:
    client = FakeQdrant()
    changes = await ensure_collections(client, "vault", 384)  # type: ignore[arg-type]
    assert sorted(client.collections) == ["kb_confidential", "kb_private", "kb_public"]
    for name in COLLECTIONS["vault"]:
        data = client.collections[name]
        dense = data["vectors"]["dense"]
        assert (dense.size, dense.distance) == (384, models.Distance.COSINE)
        assert data["sparse"]["sparse"].modifier == models.Modifier.IDF
        assert set(data["payload"]) == set(PAYLOAD_INDEXES)
        assert name in changes


async def test_second_start_changes_nothing() -> None:
    client = FakeQdrant()
    await ensure_collections(client, "vault", 384)  # type: ignore[arg-type]
    writes = list(client.writes)
    assert await ensure_collections(client, "vault", 384) == []  # type: ignore[arg-type]
    assert client.writes == writes


async def test_public_profile_creates_only_kb_public() -> None:
    client = FakeQdrant()
    await ensure_collections(client, "public", 384)  # type: ignore[arg-type]
    assert list(client.collections) == ["kb_public"]


async def test_missing_payload_index_is_added_alone() -> None:
    client = FakeQdrant()
    await ensure_collections(client, "public", 384)  # type: ignore[arg-type]
    del client.collections["kb_public"]["payload"]["category"]
    changes = await ensure_collections(client, "public", 384)  # type: ignore[arg-type]
    assert changes == ["kb_public.category"]


async def test_wrong_dimension_fails_startup() -> None:
    client = FakeQdrant()
    await ensure_collections(client, "public", 384)  # type: ignore[arg-type]
    with pytest.raises(StorageStartupError, match="dense size 384"):
        await ensure_collections(client, "public", 768)  # type: ignore[arg-type]
