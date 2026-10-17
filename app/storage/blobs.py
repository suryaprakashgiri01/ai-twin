"""Encrypted, content-addressed store for raw files.

A blob lives at `<root>/<sha[:2]>/<sha>`, where `sha` is the SHA-256 of the plaintext, so the
same file is stored once. On disk it is a Fernet token (AES-128-CBC + HMAC-SHA256): unreadable
and tamper-evident without `BLOB_ENCRYPTION_KEY`. Calls are synchronous file I/O; async callers
wrap them in `asyncio.to_thread`.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from app.db.documents import BlobRef
from app.exceptions import AppError, ErrorCode


class BlobIntegrityError(RuntimeError):
    """A blob failed to decrypt with this key, or decrypted to the wrong content."""


class BlobStore:
    def __init__(self, root: Path, key: str | bytes) -> None:
        if not key:
            raise ValueError("BLOB_ENCRYPTION_KEY is empty; run the setup script")
        self._root = root
        self._fernet = Fernet(key)

    @property
    def root(self) -> Path:
        return self._root

    def path_for(self, sha256: str) -> Path:
        if len(sha256) != 64 or not all(c in "0123456789abcdef" for c in sha256):
            raise ValueError("not a SHA-256 hex digest")
        return self._root / sha256[:2] / sha256

    def put(self, data: bytes, content_type: str) -> BlobRef:
        sha = hashlib.sha256(data).hexdigest()
        ref = BlobRef(sha256=sha, size=len(data), content_type=content_type)
        path = self.path_for(sha)
        if path.exists():
            return ref
        path.parent.mkdir(parents=True, exist_ok=True)
        self._write_atomic(path, self._fernet.encrypt(data))
        return ref

    def get(self, ref: BlobRef) -> bytes:
        path = self.path_for(ref.sha256)
        if not path.exists():
            raise AppError(ErrorCode.NOT_FOUND, "Blob not found")
        return self.decrypt_file(path, ref.sha256)

    def delete(self, ref: BlobRef) -> bool:
        path = self.path_for(ref.sha256)
        if not path.exists():
            return False
        path.unlink()
        return True

    def decrypt_file(self, path: Path, sha256: str) -> bytes:
        try:
            data = self._fernet.decrypt(path.read_bytes())
        except InvalidToken as exc:
            raise BlobIntegrityError(f"blob {sha256[:12]} does not decrypt with this key") from exc
        if hashlib.sha256(data).hexdigest() != sha256:
            raise BlobIntegrityError(f"blob {sha256[:12]} content does not match its address")
        return data

    def iter_shas(self) -> list[str]:
        """Every stored blob's address, sorted."""
        if not self._root.exists():
            return []
        return sorted(p.name for p in self._root.glob("??/*") if len(p.name) == 64)

    @staticmethod
    def _write_atomic(path: Path, payload: bytes) -> None:
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
