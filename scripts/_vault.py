"""Shared pieces of vault export/import: stack access, manifest, archive packing.

MongoDB is dumped and restored with the tools inside the mongo container (`compose exec -T`), so
neither machine needs them installed. Credentials come from that container's own environment;
they never appear on the host command line. Qdrant goes through its HTTP snapshot API.
"""

from __future__ import annotations

import getpass
import hashlib
import json
import os
import shutil
import tarfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
from pymongo import MongoClient

from app.core.config import Settings
from scripts._common import ENV_FILE, ROOT
from scripts._crypto import DecryptingReader, EncryptingWriter

FORMAT_VERSION = 1
MANIFEST = "manifest.json"
MONGO_PART = "mongo.archive.gz"
BLOB_KEY_PART = "blob-key.txt"
QDRANT_DIR = "qdrant"
BLOBS_DIR = "blobs"
EXPORTS_DIR = ROOT / "vault" / "exports"
PASSPHRASE_ENV = "EXPORT_PASSPHRASE"
MIN_PASSPHRASE = 12

# Shell run inside the mongo container; "$1" is the database name.
_AUTH = (
    '--username "$MONGO_INITDB_ROOT_USERNAME" --password "$MONGO_INITDB_ROOT_PASSWORD" '
    "--authenticationDatabase admin"
)
MONGODUMP = ["sh", "-c", f'exec mongodump --quiet --archive --gzip {_AUTH} --db "$1"', "sh"]
MONGORESTORE = [
    "sh",
    "-c",
    f'exec mongorestore --quiet --archive --gzip --drop {_AUTH} --nsInclude "$1.*"',
    "sh",
]


def load_settings() -> Settings:
    return Settings(_env_file=ENV_FILE)


def blob_root(settings: Settings) -> Path:
    path = settings.blob_dir
    return path if path.is_absolute() else ROOT / path


def read_passphrase(*, confirm: bool) -> str:
    """From EXPORT_PASSPHRASE (automation) or a prompt. Never stored, never printed."""
    value = os.environ.get(PASSPHRASE_ENV)
    if value is None:
        value = getpass.getpass("Archive passphrase: ")
        if confirm and getpass.getpass("Repeat passphrase: ") != value:
            raise SystemExit("Passphrases do not match.")
    if len(value) < MIN_PASSPHRASE:
        raise SystemExit(f"The passphrase needs at least {MIN_PASSPHRASE} characters.")
    return value


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


# --- live stack ---------------------------------------------------------------------------


class Qdrant:
    def __init__(self, settings: Settings) -> None:
        key = settings.qdrant_api_key.get_secret_value()
        self._http = httpx.Client(
            base_url=settings.qdrant_url.rstrip("/"),
            headers={"api-key": key} if key else {},
            timeout=httpx.Timeout(30.0, read=600.0),
        )

    def close(self) -> None:
        self._http.close()

    def _json(self, method: str, path: str, **kwargs: Any) -> Any:
        response = self._http.request(method, path, **kwargs)
        response.raise_for_status()
        return response.json()

    def version(self) -> str:
        return str(self._json("GET", "/")["version"])

    def collections(self) -> list[str]:
        result = self._json("GET", "/collections")["result"]["collections"]
        return sorted(c["name"] for c in result)

    def count(self, name: str) -> int:
        body = self._json("POST", f"/collections/{name}/points/count", json={"exact": True})
        return int(body["result"]["count"])

    def snapshot_to(self, name: str, target: Path) -> None:
        created = self._json("POST", f"/collections/{name}/snapshots", params={"wait": "true"})
        snapshot = created["result"]["name"]
        try:
            with (
                self._http.stream("GET", f"/collections/{name}/snapshots/{snapshot}") as response,
                target.open("wb") as handle,
            ):
                response.raise_for_status()
                for chunk in response.iter_bytes():
                    handle.write(chunk)
        finally:
            self._http.delete(f"/collections/{name}/snapshots/{snapshot}")

    def restore(self, name: str, snapshot: Path) -> None:
        with snapshot.open("rb") as handle:
            self._json(
                "POST",
                f"/collections/{name}/snapshots/upload",
                params={"priority": "snapshot", "wait": "true"},
                files={"snapshot": (snapshot.name, handle, "application/octet-stream")},
            )


def mongo_state(settings: Settings) -> tuple[str, dict[str, int]]:
    """Server version and per-collection document counts for the app database."""
    client: MongoClient[dict[str, Any]] = MongoClient(
        settings.mongodb_uri, serverSelectionTimeoutMS=settings.mongodb_timeout_ms
    )
    try:
        db = client[settings.mongodb_database]
        counts = {name: db[name].count_documents({}) for name in sorted(db.list_collection_names())}
        return str(client.server_info()["version"]), counts
    finally:
        client.close()


def qdrant_counts(qdrant: Qdrant) -> dict[str, int]:
    return {name: qdrant.count(name) for name in qdrant.collections()}


# --- manifest and archive -----------------------------------------------------------------


def part_checksums(staging: Path) -> dict[str, dict[str, Any]]:
    parts: dict[str, dict[str, Any]] = {}
    for path in sorted(p for p in staging.rglob("*") if p.is_file()):
        rel = path.relative_to(staging).as_posix()
        if rel != MANIFEST:
            parts[rel] = {"sha256": sha256_file(path), "size": path.stat().st_size}
    return parts


def verify_parts(staging: Path, manifest: dict[str, Any]) -> list[str]:
    """Every listed part present with its checksum, nothing unlisted. Returns the problems."""
    expected: dict[str, dict[str, Any]] = manifest.get("parts", {})
    actual = part_checksums(staging)
    problems = [f"missing part {name}" for name in sorted(set(expected) - set(actual))]
    problems += [f"unlisted part {name}" for name in sorted(set(actual) - set(expected))]
    problems += [
        f"checksum mismatch for {name}"
        for name in sorted(set(expected) & set(actual))
        if expected[name]["sha256"] != actual[name]["sha256"]
    ]
    return problems


def pack(staging: Path, archive: Path, passphrase: str) -> None:
    """Stream the staging folder into an encrypted tar; manifest first."""
    archive.parent.mkdir(parents=True, exist_ok=True)
    tmp = archive.with_suffix(archive.suffix + ".partial")
    try:
        with (
            tmp.open("wb") as sink,
            EncryptingWriter(sink, passphrase) as writer,
            tarfile.open(fileobj=writer, mode="w|") as tar,
        ):
            tar.add(staging / MANIFEST, arcname=MANIFEST)
            for path in sorted(p for p in staging.rglob("*") if p.is_file()):
                rel = path.relative_to(staging).as_posix()
                if rel != MANIFEST:
                    tar.add(path, arcname=rel)
        tmp.chmod(0o600)  # owner-only, like .env (no effect on Windows ACLs)
        tmp.replace(archive)
    finally:
        tmp.unlink(missing_ok=True)


def unpack(archive: Path, staging: Path, passphrase: str) -> dict[str, Any]:
    """Decrypt and extract into `staging`; returns the manifest. Raises ArchiveError."""
    with archive.open("rb") as source:
        reader = DecryptingReader(source, passphrase)
        with tarfile.open(fileobj=reader, mode="r|") as tar:
            tar.extractall(staging, filter="data")
    manifest_path = staging / MANIFEST
    if not manifest_path.exists():
        return {}
    manifest: dict[str, Any] = json.loads(manifest_path.read_text(encoding="utf-8"))
    return manifest


@contextmanager
def staging_dir(prefix: str) -> Iterator[Path]:
    """Scratch space inside the git-ignored vault folder, always removed afterwards."""
    EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
    path = EXPORTS_DIR / f".{prefix}-{os.getpid()}"
    shutil.rmtree(path, ignore_errors=True)
    path.mkdir(mode=0o700)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)
