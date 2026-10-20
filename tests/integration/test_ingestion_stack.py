"""Ingestion against the compose stack with the real local models (spaCy, fastembed).

Uses the throwaway `ai_twin_it` database; vectors land in the real Qdrant collections and are
removed per document at the end of each test.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from beanie import PydanticObjectId

from app.core.config import Settings
from app.core.resources import Resources
from app.db.documents import IngestMeta, SourceFields
from app.kb.embedding import DENSE_DIM, Embedding, FastEmbedder
from app.kb.ingestion.fetch.github import GitHubFetcher
from app.kb.ingestion.fetch.url import UrlFetcher
from app.kb.ingestion.guard import EmployerGuard
from app.kb.ingestion.jobs import IngestionService, WorkerPool
from app.kb.ingestion.labelling import Labeller
from app.kb.ingestion.pipeline import Pipeline
from app.kb.ingestion.redaction import Redactor, default_detector
from app.kb.publishing import Publisher
from app.kb.vectors import ALL_COLLECTIONS
from app.migrations import m0002_ingestion_fields, m0007_public_ids
from app.services.model_overrides import OrchestratorOverrides, OverrideIn
from app.services.profile import ProfileIn, RecordIn
from llm_orchestrator import (
    Inventory,
    Orchestrator,
    OrchestratorSettings,
    ProviderSecrets,
    Role,
    Tier,
)
from tests.ingestion import persona
from tests.ingestion.test_chunk_label import SpyClassifier

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]
DENYLIST = ROOT / "config" / "denylist.example.yaml"


@dataclass
class Stack:
    res: Resources
    pipeline: Pipeline
    service: IngestionService
    publisher: Publisher
    classifier: SpyClassifier
    created: list[str]

    async def ingest(self, body: str, **meta: object) -> PydanticObjectId:
        ref = self.res.blobs.put(body.encode(), "text/markdown")
        fields = SourceFields(
            kind="text", checksum=ref.sha256, blob=ref, meta=IngestMeta.model_validate(meta)
        )
        source, _ = await self.service.submit(fields)
        await self.service.pool.join()
        return source.id

    async def documents(self, source_id: PydanticObjectId) -> list[PydanticObjectId]:
        source = await self.res.kb.get_source(source_id)
        assert source is not None
        assert source.status in ("done", "quarantined"), source.error_message
        ids = list(source.document_ids)
        self.created += [str(i) for i in ids]
        return ids


def _models(settings: Settings) -> Path:
    path = ROOT / "vault" / "models"
    if not (path / "spacy" / "en_core_web_md" / "config.cfg").exists():
        pytest.skip("local models missing: uv run python -m scripts.models --embeddings")
    return path


@pytest.fixture
async def stack(resources: Resources, settings: Settings) -> AsyncIterator[Stack]:
    models = _models(settings)
    inventory = Inventory.from_yaml(settings.model_inventory_path)
    orchestrator = Orchestrator.from_config(
        inventory, OrchestratorSettings(secrets=ProviderSecrets(mock_enabled=True))
    )
    classifier = SpyClassifier("private")
    pipeline = Pipeline(
        store=resources.kb,
        vectors=resources.vectors,
        blobs=resources.blobs,
        redactor=lambda: Redactor(
            default_detector(models / "spacy" / "en_core_web_md"),
            allowlist=[persona.PUBLIC_EMAIL, persona.NAME],
        ),
        guard=EmployerGuard(DENYLIST),
        labeller=Labeller(orchestrator, classifier),
        embedder=FastEmbedder(models / "fastembed"),
        url_fetcher=UrlFetcher(max_bytes=1 << 20),
        github=GitHubFetcher(frozenset()),
    )
    pool = WorkerPool(pipeline.run, 2)
    pool.start()
    created: list[str] = []
    yield Stack(
        resources,
        pipeline,
        IngestionService(resources.kb, pool),
        Publisher(resources.kb, resources.vectors, pipeline),
        classifier,
        created,
    )
    await pool.stop()
    for document_id in created:
        await pipeline.delete_vectors(document_id)


async def total(stack: Stack, document_id: PydanticObjectId) -> dict[str, int]:
    return {c: await stack.res.vectors.count(c, str(document_id)) for c in ALL_COLLECTIONS}


async def test_full_flow_redact_index_publish_unpublish_delete(stack: Stack) -> None:
    text = f"{persona.NAME} paired with Rahul Sharma. " + persona.sensitive_note()
    [doc_id] = await stack.documents(await stack.ingest(text, category="experience"))
    doc = await stack.res.kb.get_document(doc_id)
    assert doc is not None and doc.tier == "private" and doc.text
    points = await stack.res.vectors.points_for("kb_private", str(doc_id))
    assert len(points) >= 2 and sum(bool(p.payload["canary"]) for p in points) == 1
    assert len(points[0].dense) == DENSE_DIM and points[0].sparse_indices
    payload = repr([p.payload for p in points])
    for raw in (*persona.RAW_VALUES, persona.aadhaar(), "Rahul Sharma"):
        assert raw not in doc.text and raw not in payload, raw
    assert persona.PUBLIC_EMAIL in doc.text and "[PERSON]" in doc.text

    await stack.publisher.update(doc_id, {"tier": "public"})
    await stack.publisher.approve(doc_id)
    _, copied = await stack.publisher.publish(doc_id)
    counts = await total(stack, doc_id)
    assert counts["kb_public"] == copied == counts["kb_private"]
    public = await stack.res.vectors.points_for("kb_public", str(doc_id))
    assert all(p.payload["published"] and not p.payload["canary"] for p in public)

    await stack.publisher.unpublish(doc_id)
    assert (await total(stack, doc_id))["kb_public"] == 0
    await stack.publisher.publish(doc_id)
    await stack.publisher.delete(doc_id)
    assert await total(stack, doc_id) == {c: 0 for c in ALL_COLLECTIONS}
    deleted = await stack.res.kb.get_document(doc_id)
    assert deleted is not None and deleted.deleted_at is not None


async def test_quarantined_and_restricted_have_no_vectors(stack: Stack) -> None:
    [quarantined] = await stack.documents(
        await stack.ingest("Closed FAKEPROJ-77 on wiki.example-corp.test", category="work-note")
    )
    [restricted] = await stack.documents(await stack.ingest("marks: 91", category="marksheet"))
    for doc_id in (quarantined, restricted):
        assert await total(stack, doc_id) == {c: 0 for c in ALL_COLLECTIONS}
        assert await stack.res.kb.chunks_for(doc_id) == []
    doc = await stack.res.kb.get_document(restricted)
    assert doc is not None and doc.text is None and doc.tier == "restricted"
    queue = await stack.res.kb.review_queue(50)
    assert quarantined in [d.id for d in queue]


async def test_same_bytes_same_source_and_unique_index(stack: Stack) -> None:
    first = await stack.ingest("Same note twice.", category="skill")
    await stack.documents(first)
    second = await stack.ingest("Same note twice.", category="skill")
    assert first == second
    count = await stack.res.mongo["ai_twin_it"]["documents"].count_documents({"source_id": first})
    assert count == 1


class KilledEmbedder:
    async def embed(self, texts: list[str]) -> list[Embedding]:
        raise asyncio.CancelledError


async def test_interrupted_job_finishes_once_after_restart(stack: Stack) -> None:
    good = stack.pipeline.embedder
    stack.pipeline.embedder = KilledEmbedder()
    ref = stack.res.blobs.put(b"Interrupted note about queues.", "text/markdown")
    source, _ = await stack.res.kb.create_source(
        SourceFields(kind="text", checksum=ref.sha256, blob=ref, meta=IngestMeta(category="skill"))
    )
    with pytest.raises(asyncio.CancelledError):
        await stack.pipeline.run(source.id)
    stack.pipeline.embedder = good
    assert await stack.service.recover() == 1
    await stack.service.pool.join()
    [doc_id] = await stack.documents(source.id)
    assert len(await stack.res.kb.documents_for_source(source.id)) == 1
    chunks = await stack.res.kb.chunks_for(doc_id)
    assert (await total(stack, doc_id))["kb_private"] == len(chunks)


async def test_migration_0002_backfills_legacy_rows(
    resources: Resources, settings: Settings
) -> None:
    db = resources.mongo[settings.mongodb_database]
    await db["sources"].insert_one({"kind": "text", "checksum": "legacy", "status": "new"})
    await m0002_ingestion_fields.up(db)
    row = await db["sources"].find_one({"checksum": "legacy"})
    assert row is not None and row["status"] == "done" and row["attempts"] == 0
    await m0002_ingestion_fields.up(db)  # idempotent


async def test_migration_0007_backfills_public_ids(
    resources: Resources, settings: Settings
) -> None:
    db = resources.mongo[settings.mongodb_database]
    source = await db["sources"].insert_one({"kind": "text", "checksum": "old-uid"})
    doc = await db["documents"].insert_one({"source_id": source.inserted_id, "title": "Old"})
    await db["profile"].update_one(
        {"_id": "profile"}, {"$set": {"resume_document_id": doc.inserted_id}}, upsert=True
    )
    await m0007_public_ids.up(db)
    src = await db["sources"].find_one({"_id": source.inserted_id})
    row = await db["documents"].find_one({"_id": doc.inserted_id})
    profile = await db["profile"].find_one({"_id": "profile"})
    assert src and row and profile
    assert uuid.UUID(str(row["uid"])).version == 4 and row["source_uid"] == src["uid"]
    assert profile["resume_document_uid"] == row["uid"]
    await m0007_public_ids.up(db)  # idempotent: nothing is re-minted
    again = await db["documents"].find_one({"_id": doc.inserted_id})
    assert again and again["uid"] == row["uid"]
    await m0007_public_ids.down(db)
    gone = await db["documents"].find_one({"_id": doc.inserted_id})
    assert gone and "uid" not in gone and "source_uid" not in gone


async def test_profile_and_records_round_trip(resources: Resources) -> None:
    saved = await resources.profile.put(ProfileIn(availability="open", locations=["Remote"]), None)
    assert (await resources.profile.get()).locations == ["Remote"] == saved.locations
    record = await resources.records.create(
        RecordIn(
            kind="interview", company="Acme Widgets", role="SDE", stage="r1", date="2026-09-01"
        )
    )
    updated = await resources.records.update(record.id, {"stage": "r2"})
    assert updated is not None and updated.stage == "r2"
    assert [r.id for r in await resources.records.list("interview")] == [record.id]
    assert await resources.records.delete(record.id)
    assert not await resources.records.delete(record.id)
    assert await resources.records.update(record.id, {"stage": "x"}) is None


async def test_mongo_override_reaches_the_orchestrator(
    resources: Resources, settings: Settings
) -> None:
    inventory = Inventory.from_yaml(settings.model_inventory_path)
    orchestrator = Orchestrator.from_config(
        inventory,
        OrchestratorSettings(
            secrets=ProviderSecrets(mock_enabled=True, ollama_base_url="http://127.0.0.1:11434")
        ),
        OrchestratorOverrides(lambda: resources.overrides),
    )

    async def chain() -> list[str]:
        return [
            m.id for m in (await orchestrator.select(Role.CLASSIFIER, Tier.CONFIDENTIAL)).models
        ]

    assert await chain() == ["ollama-local", "mock"]
    await resources.overrides.put("ollama-local", OverrideIn(enabled=False))
    assert await chain() == ["mock"]


async def test_real_url_fetch_of_a_public_page() -> None:
    try:
        fetched = await UrlFetcher(max_bytes=1 << 20).fetch("https://example.com/")
    except Exception as exc:  # pragma: no cover - offline machine
        pytest.skip(f"network unavailable: {type(exc).__name__}")
    assert fetched.content_type == "text/html"
    assert b"Example Domain" in fetched.data
    with pytest.raises(Exception, match="private"):
        await UrlFetcher(max_bytes=1024).fetch("http://localhost:8000/")
