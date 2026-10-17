from __future__ import annotations

from typing import Any

import pytest

from app.kb.ingestion.redaction import PresidioDetector, Redactor
from tests.ingestion import persona


@pytest.fixture(scope="session")
def blank_detector() -> PresidioDetector:
    """Presidio on a blank spaCy pipeline: every pattern recogniser, no NER model download."""
    import spacy

    nlp: Any = spacy.blank("en")
    return PresidioDetector(nlp)


@pytest.fixture
def redactor(blank_detector: PresidioDetector) -> Redactor:
    return Redactor(blank_detector, allowlist=[persona.PUBLIC_EMAIL])


# --- pipeline harness ---------------------------------------------------------------------------

import hashlib  # noqa: E402
from collections.abc import AsyncIterator  # noqa: E402
from dataclasses import dataclass  # noqa: E402
from pathlib import Path  # noqa: E402

import httpx  # noqa: E402
from cryptography.fernet import Fernet  # noqa: E402

from app.db.documents import IngestMeta, SourceFields  # noqa: E402
from app.kb.ingestion.fetch.github import GitHubFetcher, parse_allowlist  # noqa: E402
from app.kb.ingestion.fetch.url import UrlFetcher  # noqa: E402
from app.kb.ingestion.guard import EmployerGuard  # noqa: E402
from app.kb.ingestion.jobs import IngestionService, WorkerPool  # noqa: E402
from app.kb.ingestion.labelling import Labeller  # noqa: E402
from app.kb.ingestion.pipeline import Pipeline  # noqa: E402
from app.storage.blobs import BlobStore  # noqa: E402
from tests.ingestion.fakes import FakeEmbedder, MemoryIndex, MemoryKbStore  # noqa: E402
from tests.ingestion.test_chunk_label import SpyClassifier, orchestrator  # noqa: E402

EXAMPLE_DENYLIST = Path(__file__).resolve().parents[2] / "config" / "denylist.example.yaml"
REPO = "kavya-testpal/rate-limiter"


def web_handler(request: httpx.Request) -> httpx.Response:
    if request.url.host == "api.github.com":
        if request.url.path.endswith("/languages"):
            return httpx.Response(200, json={"Go": 10})
        if request.url.path.endswith("/readme"):
            return httpx.Response(200, text="# Rate limiter\n\nToken bucket.")
        return httpx.Response(200, json={"full_name": REPO, "description": "A rate limiter"})
    page = (
        "<html><body><h1>Talk notes</h1><p>Consistent hashing explained by "
        f"{persona.NAME}.</p></body></html>"
    )
    return httpx.Response(200, text=page, headers={"content-type": "text/html"})


async def public_dns(host: str, port: int) -> list[str]:
    return ["93.184.215.14"]


class FakeOcrEngine:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, image: bytes) -> list[str]:
        self.calls += 1
        return ["Screenshot text"]


@dataclass
class Harness:
    store: MemoryKbStore
    index: MemoryIndex
    blobs: BlobStore
    embedder: FakeEmbedder
    classifier: SpyClassifier
    ocr: FakeOcrEngine
    pipeline: Pipeline
    service: IngestionService

    def text_source(self, body: str, **meta: object) -> SourceFields:
        ref = self.blobs.put(body.encode(), "text/markdown")
        return SourceFields(
            kind="text", checksum=ref.sha256, blob=ref, meta=IngestMeta.model_validate(meta)
        )

    def file_source(self, data: bytes, filename: str, **meta: object) -> SourceFields:
        ref = self.blobs.put(data, "application/octet-stream")
        return SourceFields(
            kind="file",
            filename=filename,
            checksum=ref.sha256,
            blob=ref,
            meta=IngestMeta.model_validate(meta),
        )

    @staticmethod
    def remote_source(kind: str, uri: str, **meta: object) -> SourceFields:
        checksum = hashlib.sha256(f"{kind}:{uri}".encode()).hexdigest()
        return SourceFields(
            kind=kind,
            uri=uri,
            checksum=checksum,
            meta=IngestMeta.model_validate(meta),
        )

    async def ingest(self, fields: SourceFields) -> tuple[str, bool]:
        source, queued = await self.service.submit(fields)
        await self.service.pool.join()
        return str(source.id), queued


@pytest.fixture
async def harness(tmp_path: Path, blank_detector: PresidioDetector) -> AsyncIterator[Harness]:
    store, index, embedder = MemoryKbStore(), MemoryIndex(), FakeEmbedder()
    blobs = BlobStore(tmp_path / "blobs", Fernet.generate_key())
    classifier, ocr = SpyClassifier("private"), FakeOcrEngine()
    transport = httpx.MockTransport(web_handler)
    pipeline = Pipeline(
        store=store,
        vectors=index,
        blobs=blobs,
        redactor=lambda: Redactor(blank_detector, allowlist=[persona.PUBLIC_EMAIL]),
        guard=EmployerGuard(EXAMPLE_DENYLIST),
        labeller=Labeller(orchestrator(mock_enabled=True), classifier),
        embedder=embedder,
        url_fetcher=UrlFetcher(max_bytes=1 << 20, resolver=public_dns, transport=transport),
        github=GitHubFetcher(parse_allowlist(REPO), transport=transport),
        ocr=ocr,
        chunk_target=120,
        chunk_overlap=20,
    )
    pool = WorkerPool(pipeline.run, workers=2)
    pool.start()
    yield Harness(
        store, index, blobs, embedder, classifier, ocr, pipeline, IngestionService(store, pool)
    )
    await pool.stop()
