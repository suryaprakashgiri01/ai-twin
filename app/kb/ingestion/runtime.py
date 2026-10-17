"""Assemble the ingestion pipeline from settings and open storage, and run its workers.

Started in the app lifespan (vault profile only): the worker pool starts, interrupted jobs are
re-queued, and on shutdown the workers stop (unfinished jobs resume at the next start).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from app.core.config import Settings, ingest_max_bytes, split_list
from app.core.llm import LLMServices
from app.core.resources import Resources
from app.kb.embedding import FastEmbedder
from app.kb.ingestion.fetch.github import GitHubFetcher, parse_allowlist
from app.kb.ingestion.fetch.url import UrlFetcher
from app.kb.ingestion.guard import EmployerGuard
from app.kb.ingestion.jobs import IngestionService, WorkerPool
from app.kb.ingestion.labelling import Labeller
from app.kb.ingestion.pipeline import Pipeline
from app.kb.ingestion.redaction import Redactor, default_detector


@dataclass(frozen=True)
class Ingestion:
    service: IngestionService
    pipeline: Pipeline


def build_ingestion(settings: Settings, resources: Resources, llm: LLMServices) -> Ingestion:
    allowlist = split_list(settings.public_contact_allowlist)
    model_path = settings.models_dir / "spacy" / "en_core_web_md"
    token = settings.github_token.get_secret_value() if settings.github_token else None
    pipeline = Pipeline(
        store=resources.kb,
        vectors=resources.vectors,
        blobs=resources.blobs,
        redactor=lambda: Redactor(default_detector(model_path), allowlist=allowlist),
        guard=EmployerGuard(settings.denylist_path),
        labeller=Labeller(llm.orchestrator, llm.classifier),
        embedder=FastEmbedder(settings.models_dir / "fastembed"),
        url_fetcher=UrlFetcher(
            max_bytes=ingest_max_bytes(settings),
            timeout_seconds=settings.url_fetch_timeout_seconds,
        ),
        github=GitHubFetcher(
            parse_allowlist(settings.github_sync_repos),
            token=token or None,
            timeout_seconds=settings.url_fetch_timeout_seconds,
        ),
        chunk_target=settings.chunk_target_tokens,
        chunk_overlap=settings.chunk_overlap_tokens,
    )
    pool = WorkerPool(pipeline.run, settings.ingest_workers)
    return Ingestion(service=IngestionService(resources.kb, pool), pipeline=pipeline)


@asynccontextmanager
async def run_ingestion(ingestion: Ingestion) -> AsyncIterator[Ingestion]:
    ingestion.service.pool.start()
    try:
        await ingestion.service.recover()
        yield ingestion
    finally:
        await ingestion.service.pool.stop()
