"""The downloadable resume: the profile's resume blob in the vault, a provisioned file in public.

The owner chooses the vault resume by pointing the profile at an ingested document; the public
deployment serves `PUBLIC_RESUME_PATH`, which deploy-public provisions. Either way, no resume
configured means 404. Downloads are counted per UTC day, with unique visitors counted by their
salted hash only (a HyperLogLog, so not even the hashes can be listed back).
"""

from __future__ import annotations

import asyncio
import mimetypes
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from redis.asyncio import Redis

from app.cache.redis import resume_downloads_key, resume_visitors_key
from app.core.config import Settings
from app.db.documents import BlobRef
from app.exceptions import AppError
from app.services.profile import ProfileStore
from app.storage.blobs import BlobStore

DOWNLOAD_NAME = "resume"
# Daily counters are kept a little over a year.
COUNTER_TTL_SECONDS = 400 * 86_400


@dataclass(frozen=True)
class ResumeFile:
    content_type: str
    filename: str
    # Exactly one is set: decrypted bytes (vault) or a file to stream (public).
    data: bytes | None = None
    path: Path | None = None


class ResumeProvider(Protocol):
    async def available(self) -> bool: ...

    async def open(self) -> ResumeFile | None: ...


def _filename(content_type: str) -> str:
    return DOWNLOAD_NAME + (mimetypes.guess_extension(content_type) or "")


class VaultResume:
    def __init__(self, profile: ProfileStore, blobs: BlobStore) -> None:
        self._profile = profile
        self._blobs = blobs

    async def available(self) -> bool:
        return (await self._profile.get()).resume_blob is not None

    async def open(self) -> ResumeFile | None:
        ref = (await self._profile.get()).resume_blob
        if ref is None:
            return None
        try:
            data = await asyncio.to_thread(self._blobs.get, BlobRef(**ref.model_dump()))
        except AppError:
            return None  # the profile points at a blob that is gone
        return ResumeFile(ref.content_type, _filename(ref.content_type), data=data)


class FileResume:
    def __init__(self, path: Path | None) -> None:
        self._path = path

    async def available(self) -> bool:
        return self._path is not None and self._path.is_file()

    async def open(self) -> ResumeFile | None:
        if not await self.available():
            return None
        assert self._path is not None
        content_type = mimetypes.guess_type(self._path.name)[0] or "application/pdf"
        return ResumeFile(content_type, _filename(content_type), path=self._path)


def resume_provider(settings: Settings, profile: ProfileStore, blobs: BlobStore) -> ResumeProvider:
    if settings.deployment_profile == "vault":
        return VaultResume(profile, blobs)
    return FileResume(settings.public_resume_path)


async def count_download(redis: Redis, visitor_hash: str, *, now: datetime | None = None) -> None:
    day = (now or datetime.now(UTC)).strftime("%Y%m%d")
    async with redis.pipeline(transaction=True) as pipe:
        pipe.incr(resume_downloads_key(day))
        pipe.expire(resume_downloads_key(day), COUNTER_TTL_SECONDS)
        pipe.pfadd(resume_visitors_key(day), visitor_hash)
        pipe.expire(resume_visitors_key(day), COUNTER_TTL_SECONDS)
        await pipe.execute()
