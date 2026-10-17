"""Passphrase encryption for vault archives: scrypt key derivation + Fernet frames.

The archive key comes from a passphrase, not from `.env`, so the archive opens on a new machine
whose `.env` was regenerated. Layout:

    MAGIC | log2(n) r p (1 byte each) | salt (16 bytes) | frame* (4-byte length + Fernet token)

Each frame's plaintext is `index (8 bytes) | last (1 byte) | data`, at most 8 MiB of data, so a
large snapshot never sits in memory whole. Fernet authenticates every frame; the index and the
`last` flag make reordering, dropping or truncating frames detectable too.
"""

from __future__ import annotations

import base64
import hashlib
import io
import os
import struct
from typing import BinaryIO

from cryptography.fernet import Fernet, InvalidToken

MAGIC = b"ATWVAULT1\n"
SALT_BYTES = 16
FRAME_BYTES = 8 * 1024 * 1024
SCRYPT_LOG2_N, SCRYPT_R, SCRYPT_P = 15, 8, 1
_LEN = struct.Struct(">I")
_FRAME_HEAD = struct.Struct(">QB")
# The largest token a full frame can produce, with headroom; anything bigger is corruption.
_MAX_TOKEN = FRAME_BYTES * 2


class ArchiveError(RuntimeError):
    """The archive cannot be opened: wrong passphrase, corrupted or tampered."""


def derive_fernet(passphrase: str, salt: bytes, log2_n: int = SCRYPT_LOG2_N) -> Fernet:
    key = hashlib.scrypt(
        passphrase.encode(),
        salt=salt,
        n=2**log2_n,
        r=SCRYPT_R,
        p=SCRYPT_P,
        maxmem=256 * 1024 * 1024,
        dklen=32,
    )
    return Fernet(base64.urlsafe_b64encode(key))


class EncryptingWriter(io.RawIOBase):
    """A write-only file object that frames and encrypts everything written to it."""

    def __init__(self, sink: BinaryIO, passphrase: str, *, log2_n: int = SCRYPT_LOG2_N) -> None:
        super().__init__()
        salt = os.urandom(SALT_BYTES)
        self._fernet = derive_fernet(passphrase, salt, log2_n)
        self._sink = sink
        self._buffer = bytearray()
        self._index = 0
        sink.write(MAGIC + bytes([log2_n, SCRYPT_R, SCRYPT_P]) + salt)

    def writable(self) -> bool:
        return True

    def write(self, data: bytes | bytearray | memoryview) -> int:  # type: ignore[override]
        self._buffer.extend(data)
        while len(self._buffer) >= FRAME_BYTES:
            self._emit(bytes(self._buffer[:FRAME_BYTES]), last=False)
            del self._buffer[:FRAME_BYTES]
        return len(data)

    def close(self) -> None:
        if not self.closed:
            self._emit(bytes(self._buffer), last=True)
            self._buffer.clear()
            self._sink.flush()
        super().close()

    def _emit(self, data: bytes, *, last: bool) -> None:
        token = self._fernet.encrypt(_FRAME_HEAD.pack(self._index, int(last)) + data)
        self._sink.write(_LEN.pack(len(token)) + token)
        self._index += 1


class DecryptingReader(io.RawIOBase):
    """A read-only file object over an archive; raises ArchiveError on any inconsistency."""

    def __init__(self, source: BinaryIO, passphrase: str) -> None:
        super().__init__()
        header = source.read(len(MAGIC) + 3 + SALT_BYTES)
        if len(header) != len(MAGIC) + 3 + SALT_BYTES or not header.startswith(MAGIC):
            raise ArchiveError("not an ai-twin vault archive")
        log2_n = header[len(MAGIC)]
        if not 10 <= log2_n <= 20:
            raise ArchiveError("archive header is corrupted")
        self._fernet = derive_fernet(passphrase, header[len(MAGIC) + 3 :], log2_n)
        self._source = source
        self._pending = b""
        self._index = 0
        self._done = False

    def readable(self) -> bool:
        return True

    def read(self, size: int | None = -1) -> bytes:
        want = size if size is not None and size >= 0 else None
        while not self._done and (want is None or len(self._pending) < want):
            self._pending += self._next_frame()
        if want is None:
            out, self._pending = self._pending, b""
        else:
            out, self._pending = self._pending[:want], self._pending[want:]
        return out

    def readinto(self, buffer: bytearray | memoryview) -> int:  # type: ignore[override]
        data = self.read(len(buffer))
        buffer[: len(data)] = data
        return len(data)

    def _next_frame(self) -> bytes:
        head = self._source.read(_LEN.size)
        if len(head) != _LEN.size:
            raise ArchiveError("archive is truncated")
        (length,) = _LEN.unpack(head)
        if length > _MAX_TOKEN:
            raise ArchiveError("archive frame is corrupted")
        token = self._source.read(length)
        if len(token) != length:
            raise ArchiveError("archive is truncated")
        try:
            plain = self._fernet.decrypt(token)
        except InvalidToken as exc:
            raise ArchiveError("wrong passphrase, or the archive was modified") from exc
        index, last = _FRAME_HEAD.unpack(plain[: _FRAME_HEAD.size])
        if index != self._index:
            raise ArchiveError("archive frames are out of order")
        self._index += 1
        if last:
            if self._source.read(1):
                raise ArchiveError("unexpected data after the end of the archive")
            self._done = True
        return plain[_FRAME_HEAD.size :]
