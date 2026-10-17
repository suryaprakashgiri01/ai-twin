"""Export the vault to one encrypted archive: `python -m scripts.export [--out PATH]`.

Contents: a `mongodump` of the app database (run inside the mongo container), one Qdrant
snapshot per collection, the encrypted blob store with its key, and `manifest.json` (SHA-256 per
part, counts, source machine, versions). The archive is encrypted with a passphrase (prompted,
or EXPORT_PASSPHRASE for automation) that is never stored. Stop the app first so counts match.
"""

from __future__ import annotations

import argparse
import json
import platform
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.storage.blobs import BlobIntegrityError, BlobStore
from scripts import _vault
from scripts._common import compose_exec_binary, detect_compose, fail, info, ok, warn


def default_archive(now: datetime | None = None) -> Path:
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
    return _vault.EXPORTS_DIR / f"ai-twin-vault-{stamp}.atv"


def blob_key_problem(source: Path, key: str) -> str | None:
    """Why the archived blobs could not be opened on import, or None when they can."""
    shas = [p.name for p in sorted(source.glob("??/*")) if len(p.name) == 64]
    if not shas:
        return None
    if not key:
        return (
            f"{len(shas)} blob(s) but BLOB_ENCRYPTION_KEY is empty; "
            "restore the .env they were written with"
        )
    try:
        store = BlobStore(source, key)
        for sha in shas:
            store.decrypt_file(store.path_for(sha), sha)
    except (ValueError, BlobIntegrityError) as exc:
        return f"blobs do not open with BLOB_ENCRYPTION_KEY ({exc}); restore the matching .env"
    return None


def stage_blobs(source: Path, staging: Path, key: str) -> list[str]:
    """Copy the encrypted blobs as they are, plus their key so a new machine can re-key them."""
    target = staging / _vault.BLOBS_DIR
    shas: list[str] = []
    if source.exists():
        for path in sorted(source.glob("??/*")):
            if len(path.name) == 64:
                dest = target / path.parent.name / path.name
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, dest)
                shas.append(path.name)
    (staging / _vault.BLOB_KEY_PART).write_text(key, encoding="utf-8")
    return shas


def build_manifest(
    *,
    staging: Path,
    database: str,
    versions: dict[str, str],
    counts: dict[str, Any],
    blobs: list[str],
) -> dict[str, Any]:
    runtime = detect_compose()
    return {
        "format": _vault.FORMAT_VERSION,
        "created_at": datetime.now(UTC).isoformat(),
        "database": database,
        "source": {
            "os": sys.platform,
            "platform": platform.platform(terse=True),
            "runtime": " ".join(runtime) if runtime else None,
            "python": platform.python_version(),
        },
        "versions": versions,
        "counts": counts,
        "blobs": blobs,
        "parts": _vault.part_checksums(staging),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, help="archive path (default: vault/exports/...)")
    args = parser.parse_args(argv)

    settings = _vault.load_settings()
    key = settings.blob_encryption_key.get_secret_value()
    # Checked before anything is dumped: an archive whose blobs cannot be decrypted is useless.
    problem = blob_key_problem(_vault.blob_root(settings), key)
    if problem:
        fail(f"Export refused: {problem}")
        return 1
    passphrase = _vault.read_passphrase(confirm=True)
    archive: Path = args.out or default_archive()
    qdrant = _vault.Qdrant(settings)
    try:
        with _vault.staging_dir("export") as staging:
            mongo_version, mongo_counts = _vault.mongo_state(settings)
            info(f"MongoDB {mongo_version}: {sum(mongo_counts.values())} documents")
            with (staging / _vault.MONGO_PART).open("wb") as handle:
                compose_exec_binary(
                    "mongodb", [*_vault.MONGODUMP, settings.mongodb_database], stdout=handle
                )

            (staging / _vault.QDRANT_DIR).mkdir()
            points = _vault.qdrant_counts(qdrant)
            for name in points:
                qdrant.snapshot_to(name, staging / _vault.QDRANT_DIR / f"{name}.snapshot")
            info(f"Qdrant {qdrant.version()}: {len(points)} collection(s)")

            blobs = stage_blobs(_vault.blob_root(settings), staging, key)
            info(f"Blobs: {len(blobs)}")

            manifest = build_manifest(
                staging=staging,
                database=settings.mongodb_database,
                versions={"mongodb": mongo_version, "qdrant": qdrant.version()},
                counts={"mongodb": mongo_counts, "qdrant": points, "blobs": len(blobs)},
                blobs=blobs,
            )
            (staging / _vault.MANIFEST).write_text(json.dumps(manifest, indent=2), "utf-8")
            _vault.pack(staging, archive, passphrase)
    finally:
        qdrant.close()

    ok(f"Archive written: {archive}")
    warn("Keep the passphrase somewhere safe; without it the archive cannot be opened.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
