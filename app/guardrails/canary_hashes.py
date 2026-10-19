"""Canary hashes: what the output guard checks answers against, without holding any marker.

In the vault the set is computed from the `canaries` collection. The public deployment reads a
synced JSON file instead, so it can detect a leaked marker without holding anything that points
at a private document. Sync format (version 1):

    {"version": 1, "algorithm": "sha256", "hashes": ["<64 hex chars>", ...]}

Each hash is SHA-256 of the marker text (`ref-xxxxxxxx-xxxxxxxx`). A source that cannot be read
raises, and the output guard then blocks: the check fails closed.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any, Protocol

from app.db.documents import Canary
from app.kb.canaries import PREFIX

SYNC_VERSION = 1
ALGORITHM = "sha256"
# The marker format `app.kb.canaries.new_marker` mints.
MARKER = re.compile(rf"\b{re.escape(PREFIX)}-[0-9a-f]{{8}}-[0-9a-f]{{8}}\b")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


class CanarySyncError(ValueError):
    """The synced file is missing, unreadable or not in the sync format."""


def marker_hash(marker: str) -> str:
    return hashlib.sha256(marker.encode()).hexdigest()


def sync_document(markers: Iterable[str]) -> dict[str, Any]:
    """The JSON body deploy-public ships: hashes only, sorted, never the markers."""
    return {
        "version": SYNC_VERSION,
        "algorithm": ALGORITHM,
        "hashes": sorted({marker_hash(m) for m in markers}),
    }


def parse_sync(data: Any) -> frozenset[str]:
    if not isinstance(data, dict) or data.get("version") != SYNC_VERSION:
        raise CanarySyncError("unsupported canary sync version")
    if data.get("algorithm") != ALGORITHM:
        raise CanarySyncError("unsupported canary hash algorithm")
    hashes = data.get("hashes")
    if not isinstance(hashes, list) or not all(
        isinstance(h, str) and _HEX64.match(h) for h in hashes
    ):
        raise CanarySyncError("canary hashes must be a list of lowercase SHA-256 hex digests")
    return frozenset(hashes)


def leaked(text: str, hashes: frozenset[str]) -> list[str]:
    """Hashes of marker-shaped tokens in `text` that are real canaries."""
    return sorted({h for m in MARKER.findall(text) if (h := marker_hash(m)) in hashes})


class CanaryHashSource(Protocol):
    async def hashes(self) -> frozenset[str]: ...


class StaticCanaryHashes:
    def __init__(self, hashes: Iterable[str] = ()) -> None:
        self._hashes = frozenset(hashes)

    async def hashes(self) -> frozenset[str]:
        return self._hashes


class FileCanaryHashes:
    """The synced JSON file, re-read when its modification time changes."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._loaded: tuple[float, frozenset[str]] | None = None

    async def hashes(self) -> frozenset[str]:
        try:
            mtime = self._path.stat().st_mtime
            if self._loaded is None or self._loaded[0] != mtime:
                data = json.loads(self._path.read_text(encoding="utf-8"))
                self._loaded = (mtime, parse_sync(data))
        except (OSError, json.JSONDecodeError) as exc:
            raise CanarySyncError(f"cannot read canary hashes: {type(exc).__name__}") from exc
        return self._loaded[1]


class MongoCanaryHashes:
    """Every marker in the vault's `canaries` collection, hashed; cached for `ttl_seconds`."""

    def __init__(
        self, ttl_seconds: float = 60.0, *, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._ttl = ttl_seconds
        self._clock = clock
        self._cached: tuple[float, frozenset[str]] | None = None

    async def hashes(self) -> frozenset[str]:
        now = self._clock()
        if self._cached is None or now - self._cached[0] >= self._ttl:
            cursor = Canary.get_pymongo_collection().find({}, {"marker": 1, "_id": 0})
            markers = [row["marker"] async for row in cursor]
            self._cached = (now, frozenset(marker_hash(m) for m in markers))
        return self._cached[1]
