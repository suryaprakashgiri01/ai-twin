from __future__ import annotations

from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from app.exceptions import AppError
from app.storage.blobs import BlobIntegrityError, BlobStore

CONTENT = b"Jordan Example - fake resume for tests"


@pytest.fixture
def store(tmp_path: Path) -> BlobStore:
    return BlobStore(tmp_path / "blobs", Fernet.generate_key())


def test_same_content_same_ref_stored_once(store: BlobStore) -> None:
    first = store.put(CONTENT, "text/plain")
    second = store.put(CONTENT, "text/plain")
    assert first == second
    assert first.size == len(CONTENT)
    assert store.iter_shas() == [first.sha256]
    assert store.get(first) == CONTENT


def test_blob_is_unreadable_without_the_key(store: BlobStore, tmp_path: Path) -> None:
    ref = store.put(CONTENT, "text/plain")
    on_disk = store.path_for(ref.sha256).read_bytes()
    assert CONTENT not in on_disk
    assert b"Jordan" not in on_disk
    other = BlobStore(tmp_path / "blobs", Fernet.generate_key())
    with pytest.raises(BlobIntegrityError):
        other.get(ref)


def test_tampered_or_swapped_blob_is_detected(store: BlobStore) -> None:
    ref = store.put(CONTENT, "text/plain")
    other = store.put(b"other", "text/plain")
    path = store.path_for(ref.sha256)
    path.write_bytes(store.path_for(other.sha256).read_bytes())
    with pytest.raises(BlobIntegrityError, match="does not match"):
        store.get(ref)
    path.write_bytes(b"garbage")
    with pytest.raises(BlobIntegrityError, match="does not decrypt"):
        store.get(ref)


def test_delete_and_missing(store: BlobStore) -> None:
    ref = store.put(CONTENT, "text/plain")
    assert store.delete(ref) is True
    assert store.delete(ref) is False
    with pytest.raises(AppError):
        store.get(ref)
    assert store.iter_shas() == []


def test_path_rejects_non_digests(store: BlobStore) -> None:
    with pytest.raises(ValueError):
        store.path_for("../../etc/passwd")
    assert store.root.name == "blobs"


def test_empty_key_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="BLOB_ENCRYPTION_KEY"):
        BlobStore(tmp_path, "")
    assert BlobStore(tmp_path / "none", Fernet.generate_key()).iter_shas() == []
