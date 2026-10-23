"""deploy-public T2: make publish mirrors kb_public and prunes, and a dry run changes nothing."""

from __future__ import annotations

from qdrant_client import AsyncQdrantClient, models

from app.kb.vectors import PUBLIC_COLLECTION
from app.vector.qdrant import DENSE, ensure_collections
from scripts import publish

DIM = 4


def point(n: int) -> models.PointStruct:
    return models.PointStruct(
        id=n,
        vector={DENSE: [0.1 * n, 0.2, 0.3, 0.4]},
        payload={"document_id": f"d{n}", "text": f"chunk {n}"},
    )


async def store(*ids: int) -> AsyncQdrantClient:
    client = AsyncQdrantClient(location=":memory:")
    await ensure_collections(client, "public", DIM)
    if ids:
        await client.upsert(PUBLIC_COLLECTION, points=[point(n) for n in ids], wait=True)
    return client


async def ids(client: AsyncQdrantClient) -> set[int]:
    records, _ = await client.scroll(PUBLIC_COLLECTION, limit=100)
    return {int(r.id) for r in records}


async def test_dry_run_changes_nothing_and_apply_mirrors_and_prunes() -> None:
    source, target = await store(1, 2, 3), await store(3, 9)
    plan = await publish.mirror(source, target, apply=False)
    assert (plan.upsert, plan.delete) == (3, 1)
    assert await ids(target) == {3, 9}
    plan = await publish.mirror(source, target, apply=True)
    assert (plan.upsert, plan.delete) == (3, 1)
    assert await ids(target) == {1, 2, 3}
    copied, _ = await target.retrieve(PUBLIC_COLLECTION, ids=[2], with_vectors=True), None
    assert copied[0].payload == {"document_id": "d2", "text": "chunk 2"}


async def test_a_fresh_target_without_the_collection_is_a_plain_copy() -> None:
    source = await store(5)
    empty = AsyncQdrantClient(location=":memory:")
    plan = await publish.mirror(source, empty, apply=False)
    assert (plan.upsert, plan.delete) == (1, 0)
