"""Ingestion T7: pipeline stages, canaries, worker pool and restart recovery (all in memory)."""

from __future__ import annotations

import asyncio

import pytest
import structlog
from beanie import PydanticObjectId

from app.kb.embedding import Embedding
from app.kb.ingestion.jobs import MAX_ATTEMPTS, IngestionService, WorkerPool
from app.kb.vectors import collection_for
from tests.ingestion import persona
from tests.ingestion.conftest import REPO, Harness
from tests.ingestion.test_fetch import linkedin_zip


def oid(value: str) -> PydanticObjectId:
    return PydanticObjectId(value)


async def test_raw_identifiers_never_leave_redaction(harness: Harness) -> None:
    with structlog.testing.capture_logs() as logs:
        source_id, queued = await harness.ingest(
            harness.text_source(persona.sensitive_note(), title="Interview", category="interview")
        )
    assert queued
    source = harness.store.sources[oid(source_id)]
    assert source.status == "done", source.error_message
    [doc] = harness.store.documents.values()
    assert doc.text is not None
    surfaces = {
        "document text": doc.text,
        "chunks": "\n".join(c.text for c in harness.store.chunks),
        "qdrant payload": harness.index.all_payload_text(),
        "classifier input": "\n".join(c["text"] for c in harness.classifier.calls),
        "embedder input": "\n".join(harness.embedder.texts),
        "logs": repr(logs),
    }
    for name, text in surfaces.items():
        for raw in persona.RAW_VALUES:
            assert raw not in text, f"{raw!r} leaked into {name}"
        assert persona.aadhaar() not in text, name
    assert persona.PUBLIC_EMAIL in doc.text
    assert "[AADHAAR]" in doc.text
    assert doc.redaction_report["PAN"] == 1
    assert harness.classifier.calls  # it did run, on redacted text


async def test_private_document_is_indexed_with_one_canary(harness: Harness) -> None:
    source_id, _ = await harness.ingest(harness.text_source(persona.note(), category="experience"))
    [doc] = harness.store.documents.values()
    assert doc.tier == "private"
    assert doc.review_status == "pending"
    assert doc.published is False
    chunks = await harness.store.chunks_for(doc.id)
    assert sum(c.canary for c in chunks) == 1
    assert len(harness.store.canaries) == 1
    points = await harness.index.points_for("kb_private", str(doc.id))
    assert len(points) == len(chunks)
    assert {p.payload["published"] for p in points} == {False}
    assert {p.payload["tier"] for p in points} == {"private"}
    canary = [p for p in points if p.payload["canary"]]
    assert len(canary) == 1
    assert next(iter(harness.store.canaries)) in canary[0].payload["text"]
    assert await harness.index.count("kb_public") == 0
    assert harness.store.sources[oid(source_id)].document_ids == [doc.id]


async def test_confidential_goes_to_its_own_collection(harness: Harness) -> None:
    await harness.ingest(harness.text_source("Offer: 40 LPA base.", category="compensation"))
    [doc] = harness.store.documents.values()
    assert doc.tier == "confidential"
    assert await harness.index.count("kb_confidential", str(doc.id)) > 0
    assert await harness.index.count("kb_private") == 0


async def test_denylisted_document_is_quarantined_without_vectors(harness: Harness) -> None:
    body = persona.note("Fixed FAKEPROJ-4411 on wiki.example-corp.test last sprint.")
    with structlog.testing.capture_logs() as logs:
        source_id, _ = await harness.ingest(harness.text_source(body, category="work-note"))
    source = harness.store.sources[oid(source_id)]
    assert source.status == "quarantined"
    [doc] = harness.store.documents.values()
    assert doc.review_status == "quarantined"
    assert doc.quarantine_rules == ["internal-domain", "ticket-keys"]
    assert harness.store.chunks == []
    assert harness.store.canaries == {}
    assert sum([await harness.index.count(c) for c in harness.index.collections]) == 0
    assert harness.classifier.calls == []
    assert "FAKEPROJ-4411" not in repr(logs) and "example-corp" not in repr(logs)
    assert any(e.get("rules") == ["internal-domain", "ticket-keys"] for e in logs)


@pytest.mark.parametrize("category", ["marksheet", "id-document"])
async def test_restricted_document_is_blob_and_metadata_only(
    harness: Harness, category: str
) -> None:
    fields = harness.file_source(persona.png_bytes(), "scan.png", category=category, summary="10th")
    source_id, _ = await harness.ingest(fields)
    source = harness.store.sources[oid(source_id)]
    assert source.status == "done"
    assert source.blob is not None and harness.blobs.get(source.blob) == persona.png_bytes()
    [doc] = harness.store.documents.values()
    assert doc.tier == "restricted"
    assert doc.text is None
    assert doc.summary == "10th"
    assert harness.store.chunks == []
    assert sum([await harness.index.count(c) for c in harness.index.collections]) == 0
    assert harness.ocr.calls == 0  # never even parsed
    assert harness.classifier.calls == []
    assert harness.embedder.texts == []


async def test_owner_hint_restricted_also_stops_before_parsing(harness: Harness) -> None:
    await harness.ingest(
        harness.text_source("anything", category="experience", tier_hint="restricted")
    )
    [doc] = harness.store.documents.values()
    assert doc.tier == "restricted" and doc.text is None


async def test_same_bytes_return_the_same_source(harness: Harness) -> None:
    first, queued = await harness.ingest(harness.text_source(persona.note()))
    again, queued_again = await harness.ingest(harness.text_source(persona.note()))
    assert first == again
    assert queued and not queued_again
    assert len(harness.store.sources) == 1
    assert len(harness.store.documents) == 1


async def test_unsupported_file_fails_with_a_code_only(harness: Harness) -> None:
    source_id, _ = await harness.ingest(harness.file_source(b"GIF89a....", "anim.gif"))
    source = harness.store.sources[oid(source_id)]
    assert source.status == "failed"
    assert source.error_code == "UNSUPPORTED_TYPE"
    assert harness.store.documents == {}


async def test_failed_source_is_retried_on_resubmit(harness: Harness) -> None:
    fields = harness.file_source(b"GIF89a....", "anim.gif")
    first, _ = await harness.ingest(fields)
    again, queued = await harness.ingest(fields)
    assert first == again and queued
    assert harness.store.sources[oid(first)].attempts == 1


async def test_unexpected_errors_are_internal(harness: Harness) -> None:
    async def boom(texts: list[str]) -> list[Embedding]:
        raise ValueError("model exploded")

    harness.pipeline.embedder.embed = boom  # type: ignore[method-assign]
    source_id, _ = await harness.ingest(harness.text_source(persona.note()))
    source = harness.store.sources[oid(source_id)]
    assert source.status == "failed"
    assert (source.error_code, source.error_message) == ("INTERNAL", "Unexpected error")
    assert harness.store.documents == {}  # partial output removed


async def test_url_source_is_fetched_stored_and_indexed(harness: Harness) -> None:
    source_id, _ = await harness.ingest(
        harness.remote_source("url", "https://talks.example/notes", category="blog")
    )
    source = harness.store.sources[oid(source_id)]
    assert source.status == "done", source.error_message
    assert source.blob is not None
    [doc] = harness.store.documents.values()
    assert doc.title == "https://talks.example/notes"
    assert doc.tier == "private"  # classifier said private; rules said public; max wins
    assert "Consistent hashing" in (doc.text or "")


async def test_github_source(harness: Harness) -> None:
    source_id, _ = await harness.ingest(harness.remote_source("github", REPO))
    assert harness.store.sources[oid(source_id)].status == "done"
    [doc] = harness.store.documents.values()
    assert doc.category == "project"
    assert doc.title == REPO
    assert await harness.index.count("kb_public") == 0


async def test_linkedin_export_makes_one_document_per_entity(harness: Harness) -> None:
    ref = harness.blobs.put(linkedin_zip(), "application/zip")
    fields = harness.text_source("x").model_copy(
        update={"kind": "linkedin_export", "checksum": ref.sha256, "blob": ref}
    )
    source_id, _ = await harness.ingest(fields)
    source = harness.store.sources[oid(source_id)]
    assert source.status == "done"
    assert len(source.document_ids) == 7
    categories = sorted(d.category for d in harness.store.documents.values())
    assert categories.count("experience") == 2


async def test_image_file_is_ocrd(harness: Harness) -> None:
    await harness.ingest(harness.file_source(persona.png_bytes(), "shot.png", category="project"))
    [doc] = harness.store.documents.values()
    assert doc.text == "Screenshot text\n"
    assert harness.ocr.calls == 1


# --- restart recovery -------------------------------------------------------------------------


class KilledEmbedder:
    """Stands in for a process kill in the middle of embedding."""

    async def embed(self, texts: list[str]) -> list[Embedding]:
        raise asyncio.CancelledError


async def test_interrupted_job_is_requeued_and_finishes_once(harness: Harness) -> None:
    good = harness.pipeline.embedder
    harness.pipeline.embedder = KilledEmbedder()
    source, created = await harness.store.create_source(harness.text_source(persona.note()))
    assert created
    with pytest.raises(asyncio.CancelledError):
        await harness.pipeline.run(source.id)
    stuck = harness.store.sources[source.id]
    assert stuck.status == "embedding"
    assert len(harness.store.documents) == 1  # a half-written document

    harness.pipeline.embedder = good
    # A fresh process: new pool, same store.
    pool = WorkerPool(harness.pipeline.run, workers=1)
    service = IngestionService(harness.store, pool)
    pool.start()
    try:
        assert await service.recover() == 1
        await pool.join()
    finally:
        await pool.stop()
    done = harness.store.sources[source.id]
    assert done.status == "done"
    assert done.attempts == 2
    [doc] = harness.store.documents.values()
    chunks = await harness.store.chunks_for(doc.id)
    assert await harness.index.count(collection_for(doc.tier), str(doc.id)) == len(chunks)
    assert len(harness.store.canaries) == 1
    assert doc.title == "Interview at Acme Widgets"
    assert await service.recover() == 0  # nothing left to do


async def test_recovery_gives_up_after_max_attempts(harness: Harness) -> None:
    source, _ = await harness.store.create_source(harness.text_source("x"))
    await harness.store.update_source(source.id, status="parsing", attempts=MAX_ATTEMPTS)
    assert await harness.service.recover() == 0
    assert harness.store.sources[source.id].status == "failed"


async def test_terminal_and_missing_sources_are_skipped(harness: Harness) -> None:
    await harness.pipeline.run(PydanticObjectId())
    source, _ = await harness.store.create_source(harness.text_source("y"))
    await harness.store.update_source(source.id, status="done")
    await harness.pipeline.run(source.id)
    assert harness.store.documents == {}


async def test_worker_survives_a_crashing_job() -> None:
    seen: list[PydanticObjectId] = []

    async def run(source_id: PydanticObjectId) -> None:
        seen.append(source_id)
        if len(seen) == 1:
            raise RuntimeError("bug")

    pool = WorkerPool(run, workers=1)
    pool.start()
    pool.start()  # idempotent
    pool.submit(PydanticObjectId())
    pool.submit(PydanticObjectId())
    await pool.join()
    await pool.stop()
    assert len(seen) == 2
