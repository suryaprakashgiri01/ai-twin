"""The ingestion pipeline: one source in, reviewed-pending documents and vectors out.

    parse ─▶ redact ─▶ employer guard ─▶ chunk ─▶ label ─▶ (restricted: stop) ─▶ embed ─▶ upsert
                              └─▶ quarantined (stop)

Redaction runs before everything that reads text, so the guard, chunks, labels, models, logs
and vectors only ever see placeholders. A restricted category stops before parsing: the blob is
kept, the text is never read. Each run first removes what an interrupted earlier run left, so
a re-queued job finishes exactly once.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass

import structlog
from beanie import PydanticObjectId

from app.core.telemetry import tracer
from app.db.documents import TERMINAL_STATUSES, ChunkFields, KbDocumentFields, TierLabel
from app.kb import canaries
from app.kb.embedding import Embedder
from app.kb.ingestion.chunking import chunk_markdown
from app.kb.ingestion.fetch import linkedin
from app.kb.ingestion.fetch.github import GitHubFetcher
from app.kb.ingestion.fetch.url import UrlFetcher
from app.kb.ingestion.guard import EmployerGuard
from app.kb.ingestion.labelling import Labeller, is_restricted, rule_label
from app.kb.ingestion.models import IngestError, IngestErrorCode, IngestItem, Parsed
from app.kb.ingestion.parsers import dispatch
from app.kb.ingestion.parsers.image import OcrEngine
from app.kb.ingestion.redaction import Redactor
from app.kb.store import DocumentView, KbStore, SourceView
from app.kb.vectors import ALL_COLLECTIONS, VectorIndex, VectorPoint, collection_for, point_id
from app.observability.metrics import INGESTION_JOBS
from app.observability.tracing import tracing
from app.storage.blobs import BlobStore

logger = structlog.get_logger(__name__)

TEXT_TYPES = {"text/html": "page.html", "text/markdown": "page.md", "text/csv": "page.csv"}


def first_heading(markdown: str) -> str | None:
    for line in markdown.splitlines():
        if line.startswith("#"):
            return line.lstrip("#").strip() or None
    return None


@dataclass
class Pipeline:
    store: KbStore
    vectors: VectorIndex
    blobs: BlobStore
    # Built on first use: loading the spaCy model takes a few seconds.
    redactor: Callable[[], Redactor]
    guard: EmployerGuard
    labeller: Labeller
    embedder: Embedder
    url_fetcher: UrlFetcher
    github: GitHubFetcher
    ocr: OcrEngine | None = None
    chunk_target: int = 400
    chunk_overlap: int = 60

    # --- job ------------------------------------------------------------------------------

    async def run(self, source_id: PydanticObjectId) -> None:
        source = await self.store.get_source(source_id)
        if source is None or source.status in TERMINAL_STATUSES:
            return
        # A job runs outside any request: its own span gives its log lines and its Langfuse
        # trace one id.
        with (
            tracer().start_as_current_span("ingestion.job", record_exception=False),
            tracing().run(
                "ingestion",
                "ingestion",
                metadata={"job_id": str(source.id), "kind": source.kind},
            ) as trace,
        ):
            status = await self._run(source)
            INGESTION_JOBS.labels(status).inc()
            trace.tag(f"status:{status}")
            trace.meta(status=status)

    async def _run(self, source: SourceView) -> str:
        await self._discard_partial(source.id)
        await self.store.update_source(
            source.id,
            status="parsing",
            attempts=source.attempts + 1,
            error_code=None,
            error_message=None,
        )
        log = logger.bind(source_id=str(source.id), kind=source.kind)
        try:
            document_ids, quarantined = await self._process(source)
        except IngestError as exc:
            log.warning("ingestion failed", error_code=exc.code.value)
            await self._fail(source.id, exc.code.value, exc.message)
            return "failed"
        except Exception as exc:
            log.error("ingestion crashed", error_type=type(exc).__name__)
            await self._fail(source.id, IngestErrorCode.INTERNAL.value, "Unexpected error")
            return "failed"
        status = "quarantined" if quarantined else "done"
        await self.store.update_source(source.id, status=status, document_ids=document_ids)
        log.info("ingestion finished", status=status, documents=len(document_ids))
        return status

    async def _fail(self, source_id: PydanticObjectId, code: str, message: str) -> None:
        await self._discard_partial(source_id)
        await self.store.update_source(
            source_id, status="failed", error_code=code, error_message=message, document_ids=[]
        )

    async def _discard_partial(self, source_id: PydanticObjectId) -> None:
        for doc in await self.store.documents_for_source(source_id):
            await self.delete_vectors(str(doc.id))
            await self.store.purge_document(doc.id)

    async def delete_vectors(self, document_id: str) -> None:
        for collection in ALL_COLLECTIONS:
            await self.vectors.delete_document(collection, document_id)

    async def _process(self, source: SourceView) -> tuple[list[PydanticObjectId], bool]:
        meta = source.meta
        if is_restricted(meta.category or "", meta.tier_hint):
            doc = await self._store_restricted(source)
            return [doc.id], False
        parsed = await self._parse(source)
        await self.store.update_source(source.id, status="redacting")
        items = [await self._redact(self._item(source, p)) for p in parsed]
        items = [self._guard(item) for item in items]
        await self.store.update_source(source.id, status="labelling")
        ready = [await self._chunk_and_label(i) if not i.quarantine_rules else i for i in items]
        await self.store.update_source(source.id, status="embedding")
        ids = [await self._store_item(source, item) for item in ready]
        return ids, any(item.quarantine_rules for item in ready)

    # --- stages ---------------------------------------------------------------------------

    async def _parse(self, source: SourceView) -> list[Parsed]:
        if source.kind == "url":
            return [await self._parse_url(source)]
        if source.kind == "github":
            parsed = await self.github.fetch(source.uri or "")
            await self._keep_blob(source, parsed.markdown.encode(), "text/markdown")
            return [parsed]
        if source.blob is None:
            raise IngestError(IngestErrorCode.INTERNAL, "Source has no stored content")
        data = await asyncio.to_thread(self.blobs.get, source.blob)
        if source.kind == "linkedin_export":
            return await asyncio.to_thread(linkedin.parse_export, data)
        if source.kind == "text":
            parsed = await asyncio.to_thread(dispatch.parse, data, "note.md")
            return [parsed.model_copy(update={"title": first_heading(parsed.markdown)})]
        return [await asyncio.to_thread(dispatch.parse, data, source.filename, ocr=self.ocr)]

    async def _parse_url(self, source: SourceView) -> Parsed:
        fetched = await self.url_fetcher.fetch(source.uri or "")
        await self._keep_blob(
            source, fetched.data, fetched.content_type or "application/octet-stream"
        )
        name = TEXT_TYPES.get(fetched.content_type)
        parsed = await asyncio.to_thread(dispatch.parse, fetched.data, name, ocr=self.ocr)
        return parsed.model_copy(update={"title": None})

    async def _keep_blob(self, source: SourceView, data: bytes, content_type: str) -> None:
        ref = await asyncio.to_thread(self.blobs.put, data, content_type)
        await self.store.update_source(source.id, blob=ref.model_dump())

    def _item(self, source: SourceView, parsed: Parsed) -> IngestItem:
        meta = source.meta
        title = (
            (meta.title if not parsed.key else None)
            or parsed.title
            or source.filename
            or source.uri
            or "Untitled"
        )
        return IngestItem(
            source_id=str(source.id),
            source_kind=source.kind,
            title=title,
            category=parsed.category or meta.category or "work-note",
            tags=list(meta.tags),
            occurred_at=meta.occurred_at,
            tier_hint=meta.tier_hint,
            summary=meta.summary,
            detected=parsed.detected,
            markdown=parsed.markdown,
        )

    async def _redact(self, item: IngestItem) -> IngestItem:
        redactor = await asyncio.to_thread(self.redactor)
        body = await asyncio.to_thread(redactor.redact, item.markdown)
        title = await asyncio.to_thread(redactor.redact, item.title)
        summary = redactor.redact(item.summary).text if item.summary else None
        report = dict(body.report)
        for key, count in title.report.items():
            report[key] = report.get(key, 0) + count
        return item.model_copy(
            update={
                "markdown": body.text,
                "title": title.text,
                "summary": summary,
                "redacted": True,
                "redaction_report": report,
            }
        )

    def _guard(self, item: IngestItem) -> IngestItem:
        rules = self.guard.matches(item.title, item.markdown)
        if rules:
            logger.warning("document quarantined", source_id=item.source_id, rules=rules)
        return item.model_copy(update={"quarantine_rules": rules})

    async def _chunk_and_label(self, item: IngestItem) -> IngestItem:
        chunks = chunk_markdown(
            item.markdown, title=item.title, target=self.chunk_target, overlap=self.chunk_overlap
        )
        return await self.labeller.label(item.model_copy(update={"chunks": chunks}))

    # --- storage --------------------------------------------------------------------------

    async def _store_restricted(self, source: SourceView) -> DocumentView:
        meta = source.meta
        fields = KbDocumentFields(
            source_id=source.id,
            title=meta.title or source.filename or "Restricted document",
            category=meta.category or "id-document",
            tags=list(meta.tags),
            tier="restricted",
            label=TierLabel(
                tier="restricted", reason="restricted category: blob only", by="rules", confidence=1
            ),
            occurred_at=meta.occurred_at,
            summary=meta.summary,
            text=None,
        )
        return await self.store.create_document(fields)

    async def _store_item(self, source: SourceView, item: IngestItem) -> PydanticObjectId:
        label = item.label
        if label is None:  # quarantined: rules only, no model saw it
            tier, reason = rule_label(item)
            label = TierLabel(tier=tier.label, reason=reason, by="rules", confidence=1)
        fields = KbDocumentFields(
            source_id=source.id,
            title=item.title,
            category=item.category,
            tags=item.tags,
            tier=label.tier,
            label=label,
            review_status="quarantined" if item.quarantine_rules else "pending",
            occurred_at=item.occurred_at,
            text=item.markdown if label.tier != "restricted" else None,
            summary=item.summary,
            quarantine_rules=item.quarantine_rules,
            redaction_report=item.redaction_report,
        )
        doc = await self.store.create_document(fields)
        if not item.quarantine_rules and label.tier != "restricted":
            await self.index(doc, item)
        return doc.id

    async def index(self, doc: DocumentView, item: IngestItem) -> int:
        """Embed and upsert a document's chunks (plus a canary) into its tier's collection."""
        chunks = list(item.chunks)
        if canaries.needs_canary(doc.tier):
            marker = canaries.new_marker()
            await self.store.add_canary(doc.id, marker)
            heading = chunks[0].heading if chunks else doc.title
            chunks.append(canaries.canary_chunk(len(chunks), heading, marker))
        if not chunks:
            return 0
        embeddings = await self.embedder.embed([c.text for c in chunks])
        document_id = str(doc.id)
        points = [
            VectorPoint(
                id=point_id(document_id, c.ordinal),
                dense=e.dense,
                sparse_indices=e.sparse_indices,
                sparse_values=e.sparse_values,
                payload={
                    "document_id": document_id,
                    "tier": doc.tier,
                    "category": doc.category,
                    "published": False,
                    "canary": c.canary,
                    "ordinal": c.ordinal,
                    "title": doc.title,
                    "text": c.text,
                },
            )
            for c, e in zip(chunks, embeddings, strict=True)
        ]
        await self.vectors.upsert(collection_for(doc.tier), points)
        await self.store.save_chunks(
            [
                ChunkFields(
                    document_id=doc.id,
                    ordinal=c.ordinal,
                    text=c.text,
                    tier=doc.tier,
                    qdrant_point_id=p.id,
                    canary=c.canary,
                )
                for c, p in zip(chunks, points, strict=True)
            ]
        )
        return len(points)

    async def relabel_and_index(self, doc: DocumentView) -> int:
        """Chunk, label and index a stored document (used when a quarantine is released)."""
        item = IngestItem(
            source_id=str(doc.source_id),
            source_kind="text",
            title=doc.title,
            category=doc.category,
            tags=doc.tags,
            markdown=doc.text or "",
            redacted=True,
            redaction_report=doc.redaction_report,
        )
        labelled = await self._chunk_and_label(item)
        assert labelled.label is not None
        await self.store.update_document(
            doc.id, tier=labelled.label.tier, label=labelled.label.model_dump()
        )
        fresh = doc.model_copy(update={"tier": labelled.label.tier})
        if labelled.label.tier == "restricted":
            return 0
        return await self.index(fresh, labelled)
