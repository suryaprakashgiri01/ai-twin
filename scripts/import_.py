"""Import a vault archive: `python -m scripts.import_ ARCHIVE [--apply] [--force]`.

Dry run by default: decrypts the archive, verifies every part's SHA-256 against the manifest and
prints what would be restored. Nothing in the stack changes. A wrong passphrase, a modified byte
or a missing part refuses the archive before any restore.

`--apply` restores into the running stack: `mongorestore --drop` (inside the mongo container),
Qdrant snapshot upload per collection, and the blobs (re-encrypted under this machine's
BLOB_ENCRYPTION_KEY when it differs). It then re-runs the app's bootstrap (indexes, migrations,
collections) and compares document counts, point counts and blob checksums with the manifest.
A Qdrant version different from the exporting machine's is refused unless `--force`.
"""

from __future__ import annotations

import argparse
import asyncio
import tarfile
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import Settings
from app.core.resources import open_resources
from app.storage.blobs import BlobIntegrityError, BlobStore
from scripts import _vault
from scripts._common import compose_exec_binary, fail, info, ok, warn
from scripts._crypto import ArchiveError


def check_manifest(manifest: dict[str, Any], database: str) -> list[str]:
    if not manifest:
        return ["manifest.json is missing"]
    problems: list[str] = []
    if manifest.get("format") != _vault.FORMAT_VERSION:
        problems.append(f"unsupported archive format {manifest.get('format')!r}")
    if manifest.get("database") != database:
        problems.append(f"archive is for database {manifest.get('database')!r}, not {database!r}")
    return problems


def open_archive(archive: Path, staging: Path, passphrase: str, database: str) -> dict[str, Any]:
    """Decrypt, extract and verify. Raises ArchiveError listing every problem found."""
    try:
        manifest = _vault.unpack(archive, staging, passphrase)
    except tarfile.TarError as exc:
        raise ArchiveError("archive content is not a valid tar stream") from exc
    problems = check_manifest(manifest, database)
    if not problems:
        problems = _vault.verify_parts(staging, manifest)
    if problems:
        raise ArchiveError("; ".join(problems))
    return manifest


def describe(manifest: dict[str, Any]) -> None:
    source = manifest.get("source", {})
    counts = manifest.get("counts", {})
    info(
        f"Archive from {source.get('os')} ({source.get('runtime')}), "
        f"created {manifest.get('created_at')}"
    )
    mongo: dict[str, int] = counts.get("mongodb", {})
    info(f"MongoDB {manifest['versions']['mongodb']}: {sum(mongo.values())} documents")
    for name, count in mongo.items():
        print(f"    {name:<20} {count}")
    qdrant: dict[str, int] = counts.get("qdrant", {})
    info(f"Qdrant {manifest['versions']['qdrant']}: {len(qdrant)} collection(s)")
    for name, count in qdrant.items():
        print(f"    {name:<20} {count} points")
    info(f"Blobs: {counts.get('blobs', 0)}")
    owner: dict[str, int] = counts.get("owner", {})
    if owner:
        info(
            f"Owner memories: {owner.get('memories_active', 0)} active, "
            f"{owner.get('memories_pending', 0)} pending · summaries: {owner.get('summaries', 0)}"
        )


def restore_blobs(staging: Path, manifest: dict[str, Any], target: BlobStore) -> None:
    source_key = (staging / _vault.BLOB_KEY_PART).read_text(encoding="utf-8").strip()
    source = Fernet(source_key) if source_key else None
    for sha in manifest.get("blobs", []):
        packed = staging / _vault.BLOBS_DIR / sha[:2] / sha
        if source is None:
            raise ArchiveError("archive has blobs but no blob key")
        try:
            data = source.decrypt(packed.read_bytes())
        except InvalidToken as exc:
            raise ArchiveError(f"blob {sha[:12]} does not decrypt with the archived key") from exc
        target.put(data, "application/octet-stream")


def verify_restore(
    settings: Settings, manifest: dict[str, Any], qdrant: _vault.Qdrant, blobs: BlobStore
) -> list[str]:
    counts = manifest["counts"]
    problems: list[str] = []
    live_mongo = _vault.mongo_state(settings)[1]
    for name in sorted(set(counts["mongodb"]) | set(live_mongo)):
        want, have = counts["mongodb"].get(name, 0), live_mongo.get(name, 0)
        if want != have:
            problems.append(f"mongodb {name}: {have} documents, archive has {want}")
    # Archives made before owner memories existed carry no owner counts.
    problems += _vault.owner_problems(counts.get("owner", {}), _vault.owner_state(settings))
    live_points = _vault.qdrant_counts(qdrant)
    for name in sorted(set(counts["qdrant"]) | set(live_points)):
        want, have = counts["qdrant"].get(name, 0), live_points.get(name, 0)
        if want != have:
            problems.append(f"qdrant {name}: {have} points, archive has {want}")
    for sha in manifest.get("blobs", []):
        try:
            blobs.decrypt_file(blobs.path_for(sha), sha)
        except (OSError, BlobIntegrityError):
            problems.append(f"blob {sha[:12]} missing or unreadable")
    return problems


async def _bootstrap(settings: Settings) -> None:
    async with open_resources(settings):
        pass


def apply(
    staging: Path, manifest: dict[str, Any], settings: Settings, qdrant: _vault.Qdrant
) -> int:
    with (staging / _vault.MONGO_PART).open("rb") as handle:
        compose_exec_binary(
            "mongodb", [*_vault.MONGORESTORE, settings.mongodb_database], stdin=handle
        )
    ok("MongoDB restored")
    for name in manifest["counts"]["qdrant"]:
        qdrant.restore(name, staging / _vault.QDRANT_DIR / f"{name}.snapshot")
    ok("Qdrant collections restored")
    blobs = BlobStore(_vault.blob_root(settings), settings.blob_encryption_key.get_secret_value())
    restore_blobs(staging, manifest, blobs)
    ok("Blobs restored under this machine's key")
    asyncio.run(_bootstrap(settings))
    ok("Indexes, migrations and collections re-applied")

    problems = verify_restore(settings, manifest, qdrant, blobs)
    for problem in problems:
        fail(problem)
    if problems:
        return 1
    ok(
        "Verified: document counts, owner memories and summaries, point counts and blob "
        "checksums match the archive"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("--apply", action="store_true", help="restore into the running stack")
    parser.add_argument("--force", action="store_true", help="accept a Qdrant version mismatch")
    args = parser.parse_args(argv)
    if not args.archive.is_file():
        fail(f"No such archive: {args.archive}")
        return 1

    settings = _vault.load_settings()
    passphrase = _vault.read_passphrase(confirm=False)
    qdrant = _vault.Qdrant(settings)
    try:
        with _vault.staging_dir("import") as staging:
            try:
                manifest = open_archive(
                    args.archive, staging, passphrase, settings.mongodb_database
                )
            except ArchiveError as exc:
                fail(f"Archive refused, nothing restored: {exc}")
                return 2
            ok("Archive decrypted; every part matches its SHA-256")
            describe(manifest)

            live, archived = qdrant.version(), manifest["versions"]["qdrant"]
            if live != archived and not args.force:
                fail(f"Qdrant here is {live}, the archive came from {archived}. Use --force.")
                return 2
            if not args.apply:
                warn("Dry run, nothing changed. Re-run with --apply to restore.")
                return 0
            return apply(staging, manifest, settings, qdrant)
    finally:
        qdrant.close()


if __name__ == "__main__":
    raise SystemExit(main())
