from __future__ import annotations

import io
import json
import struct
from pathlib import Path
from typing import Any

import pytest
from cryptography.fernet import Fernet

from app.db.documents import BlobRef
from app.storage.blobs import BlobStore
from scripts import _crypto, _vault, export, import_
from scripts._crypto import ArchiveError, DecryptingReader, EncryptingWriter

PASS = "correct horse battery staple"


def _encrypt(data: bytes, passphrase: str = PASS) -> bytes:
    sink = io.BytesIO()
    with EncryptingWriter(sink, passphrase, log2_n=10) as writer:
        writer.write(data)
    return sink.getvalue()


def _decrypt(blob: bytes, passphrase: str = PASS) -> bytes:
    return DecryptingReader(io.BytesIO(blob), passphrase).read()


@pytest.fixture
def small_frames(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_crypto, "FRAME_BYTES", 64)


def _frames(blob: bytes) -> tuple[bytes, list[bytes]]:
    head_len = len(_crypto.MAGIC) + 3 + _crypto.SALT_BYTES
    head, rest, frames = blob[:head_len], blob[head_len:], []
    while rest:
        (n,) = struct.unpack(">I", rest[:4])
        frames.append(rest[: 4 + n])
        rest = rest[4 + n :]
    return head, frames


def test_round_trip_across_frames(small_frames: None) -> None:
    data = bytes(range(256)) * 3
    blob = _encrypt(data)
    assert len(_frames(blob)[1]) > 3
    assert data not in blob
    assert _decrypt(blob) == data
    reader = DecryptingReader(io.BytesIO(blob), PASS)
    assert reader.read(10) + reader.read() == data


def test_wrong_passphrase_and_flipped_byte_are_refused() -> None:
    blob = _encrypt(b"vault contents")
    with pytest.raises(ArchiveError, match="wrong passphrase"):
        _decrypt(blob, "not the passphrase")
    tampered = bytearray(blob)
    tampered[-10] ^= 1
    with pytest.raises(ArchiveError):
        _decrypt(bytes(tampered))


def test_truncated_reordered_and_padded_archives_are_refused(small_frames: None) -> None:
    head, frames = _frames(_encrypt(b"x" * 300))
    with pytest.raises(ArchiveError, match="truncated"):
        _decrypt(head + b"".join(frames[:-1]))
    with pytest.raises(ArchiveError, match="out of order"):
        _decrypt(head + frames[1] + frames[0] + b"".join(frames[2:]))
    with pytest.raises(ArchiveError, match="after the end"):
        _decrypt(head + b"".join(frames) + b"junk")
    with pytest.raises(ArchiveError, match="not an ai-twin"):
        _decrypt(b"PK\x03\x04 zip file")


def _staged(root: Path, blobs_key: bytes) -> tuple[Path, dict[str, Any], str]:
    """A staging folder shaped like an export, with one real encrypted blob."""
    staging = root / "staging"
    staging.mkdir()
    (staging / _vault.MONGO_PART).write_bytes(b"fake mongodump")
    (staging / _vault.QDRANT_DIR).mkdir()
    (staging / _vault.QDRANT_DIR / "kb_public.snapshot").write_bytes(b"fake snapshot")
    source = BlobStore(root / "source-blobs", blobs_key)
    ref = source.put(b"fake resume", "text/plain")
    shas = export.stage_blobs(source.root, staging, blobs_key.decode())
    manifest = export.build_manifest(
        staging=staging,
        database="ai_twin",
        versions={"mongodb": "8.0.0", "qdrant": "1.19.2"},
        counts={"mongodb": {"leads": 1}, "qdrant": {"kb_public": 0}, "blobs": len(shas)},
        blobs=shas,
    )
    (staging / _vault.MANIFEST).write_text(json.dumps(manifest), encoding="utf-8")
    return staging, manifest, ref.sha256


def test_archive_round_trip_verifies_and_rekeys_blobs(tmp_path: Path) -> None:
    key = Fernet.generate_key()
    staging, manifest, sha = _staged(tmp_path, key)
    archive = tmp_path / "out" / "vault.atv"
    _vault.pack(staging, archive, PASS)
    assert oct(archive.stat().st_mode & 0o777) == "0o600"

    target = tmp_path / "restore"
    target.mkdir()
    opened = import_.open_archive(archive, target, PASS, "ai_twin")
    assert opened["parts"] == manifest["parts"]
    assert set(opened["parts"]) >= {_vault.MONGO_PART, "qdrant/kb_public.snapshot"}

    new_machine = BlobStore(tmp_path / "new-blobs", Fernet.generate_key())
    import_.restore_blobs(target, opened, new_machine)
    assert new_machine.iter_shas() == [sha]
    ref = BlobRef(sha256=sha, size=0, content_type="text/plain")
    assert new_machine.get(ref) == b"fake resume"


def test_modified_part_is_refused_before_restore(tmp_path: Path) -> None:
    staging, _, _ = _staged(tmp_path, Fernet.generate_key())
    # A part changed after the manifest was written: the archive itself decrypts fine.
    (staging / _vault.MONGO_PART).write_bytes(b"swapped dump")
    (staging / "extra.bin").write_bytes(b"smuggled")
    archive = tmp_path / "vault.atv"
    _vault.pack(staging, archive, PASS)
    target = tmp_path / "restore"
    target.mkdir()
    with pytest.raises(ArchiveError) as err:
        import_.open_archive(archive, target, PASS, "ai_twin")
    assert "checksum mismatch for mongo.archive.gz" in str(err.value)
    assert "unlisted part extra.bin" in str(err.value)


def test_manifest_checks() -> None:
    assert import_.check_manifest({}, "ai_twin") == ["manifest.json is missing"]
    problems = import_.check_manifest({"format": 99, "database": "other"}, "ai_twin")
    assert len(problems) == 2


def test_import_refuses_tampered_archive_without_touching_the_stack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    staging, _, _ = _staged(tmp_path, Fernet.generate_key())
    archive = tmp_path / "vault.atv"
    _vault.pack(staging, archive, PASS)
    data = bytearray(archive.read_bytes())
    data[len(data) // 2] ^= 0xFF
    archive.write_bytes(bytes(data))

    calls: list[str] = []

    class NoQdrant:
        def __init__(self, _settings: object) -> None: ...

        def version(self) -> str:
            calls.append("version")
            return "1.19.2"

        def close(self) -> None: ...

    monkeypatch.setenv(_vault.PASSPHRASE_ENV, PASS)
    monkeypatch.setattr(_vault, "Qdrant", NoQdrant)
    monkeypatch.setattr(_vault, "EXPORTS_DIR", tmp_path / "exports")

    def fake_apply(*_args: object) -> int:
        calls.append("apply")
        return 0

    monkeypatch.setattr(import_, "apply", fake_apply)
    monkeypatch.setattr(import_, "compose_exec_binary", lambda *a, **k: calls.append("exec"))
    assert import_.main([str(archive), "--apply"]) == 2
    assert calls == []


def test_short_passphrase_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_vault.PASSPHRASE_ENV, "short")
    with pytest.raises(SystemExit):
        _vault.read_passphrase(confirm=False)
    monkeypatch.setenv(_vault.PASSPHRASE_ENV, PASS)
    assert _vault.read_passphrase(confirm=True) == PASS


def test_default_archive_name_is_timestamped() -> None:
    assert export.default_archive().name.startswith("ai-twin-vault-")
    assert export.default_archive().suffix == ".atv"


def test_export_refuses_blobs_without_a_usable_key(tmp_path: Path) -> None:
    key = Fernet.generate_key()
    blobs = BlobStore(tmp_path / "blobs", key)
    assert export.blob_key_problem(blobs.root, "") is None  # nothing stored yet
    blobs.put(b"fake resume", "text/plain")
    assert export.blob_key_problem(blobs.root, key.decode()) is None
    empty = export.blob_key_problem(blobs.root, "")
    assert empty is not None and "BLOB_ENCRYPTION_KEY is empty" in empty
    wrong = export.blob_key_problem(blobs.root, Fernet.generate_key().decode())
    assert wrong is not None and "do not open" in wrong


def test_verify_restore_names_lost_owner_memories_and_summaries(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    owner = {"memories_active": 2, "memories_pending": 1, "memories_archived": 0, "summaries": 3}
    manifest: dict[str, Any] = {
        "counts": {"mongodb": {"owner_memories": 3}, "qdrant": {}, "owner": owner},
        "blobs": [],
    }

    class NoPoints:
        pass

    monkeypatch.setattr(_vault, "mongo_state", lambda _s: ("8.0", {"owner_memories": 3}))
    monkeypatch.setattr(_vault, "qdrant_counts", lambda _q: {})
    blobs = BlobStore(tmp_path / "blobs", Fernet.generate_key())
    monkeypatch.setattr(_vault, "owner_state", lambda _s: dict(owner))
    assert import_.verify_restore(None, manifest, NoPoints(), blobs) == []  # type: ignore[arg-type]
    restored = {**owner, "memories_pending": 0, "memories_archived": 1, "summaries": 0}
    monkeypatch.setattr(_vault, "owner_state", lambda _s: restored)
    problems = import_.verify_restore(None, manifest, NoPoints(), blobs)  # type: ignore[arg-type]
    assert problems == [
        "owner memories_archived: 1, archive has 0",
        "owner memories_pending: 0, archive has 1",
        "owner summaries: 0, archive has 3",
    ]
    # An archive from before owner memories carries no owner counts and is not refused.
    old = {"counts": {"mongodb": {"owner_memories": 3}, "qdrant": {}}, "blobs": []}
    assert import_.verify_restore(None, old, NoPoints(), blobs) == []  # type: ignore[arg-type]
