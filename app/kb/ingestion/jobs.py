"""In-process async worker pool for ingestion jobs, with restart recovery.

Job state lives on the `Source` document, so a restart loses nothing: at startup every source
not in a terminal state is re-queued (up to `MAX_ATTEMPTS` runs, then marked failed).
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable

import structlog
from beanie import PydanticObjectId

from app.db.documents import SourceFields
from app.kb.ingestion.models import IngestErrorCode
from app.kb.store import KbStore, SourceView

logger = structlog.get_logger(__name__)

MAX_ATTEMPTS = 3

Runner = Callable[[PydanticObjectId], Awaitable[None]]


class WorkerPool:
    def __init__(self, run: Runner, workers: int) -> None:
        self._run = run
        self._workers = workers
        self._queue: asyncio.Queue[PydanticObjectId] = asyncio.Queue()
        self._tasks: list[asyncio.Task[None]] = []

    def start(self) -> None:
        if not self._tasks:
            self._tasks = [
                asyncio.create_task(self._work(n), name=f"ingest-worker-{n}")
                for n in range(self._workers)
            ]

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks = []

    def submit(self, source_id: PydanticObjectId) -> None:
        self._queue.put_nowait(source_id)

    async def join(self) -> None:
        """Wait until every submitted job has finished (tests and graceful drains)."""
        await self._queue.join()

    async def _work(self, number: int) -> None:
        while True:
            source_id = await self._queue.get()
            try:
                await self._run(source_id)
            except Exception as exc:  # the pipeline records failures; this is a last resort
                logger.error("ingest worker error", worker=number, error_type=type(exc).__name__)
            finally:
                self._queue.task_done()


class IngestionService:
    """Submits sources (deduplicated by checksum) and recovers unfinished jobs at startup."""

    def __init__(self, store: KbStore, pool: WorkerPool) -> None:
        self.store = store
        self.pool = pool

    async def submit(
        self, fields: SourceFields, *, refresh: bool = False
    ) -> tuple[SourceView, bool]:
        """Returns the source and whether a job was queued. Same bytes, same source.

        `refresh` re-runs a finished source (a URL or repository whose content changed): the
        job first removes the documents and vectors the last run made, then ingests afresh."""
        source, created = await self.store.create_source(fields)
        if created:
            self.pool.submit(source.id)
            return source, True
        refreshable = refresh and source.status in ("done", "quarantined")
        if source.status == "failed" or refreshable:  # a retry or a refresh
            await self.store.update_source(source.id, status="queued", attempts=0)
            self.pool.submit(source.id)
            return source, True
        return source, False

    async def recover(self) -> int:
        """Re-queue jobs a restart interrupted. Returns how many were re-queued."""
        requeued = 0
        for source in await self.store.unfinished_sources():
            if source.attempts >= MAX_ATTEMPTS:
                await self.store.update_source(
                    source.id,
                    status="failed",
                    error_code=IngestErrorCode.INTERNAL.value,
                    error_message=f"Gave up after {MAX_ATTEMPTS} interrupted attempts",
                )
                continue
            await self.store.update_source(source.id, status="queued")
            self.pool.submit(source.id)
            requeued += 1
        if requeued:
            logger.info("ingestion jobs re-queued", count=requeued)
        return requeued
