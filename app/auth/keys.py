"""API keys: `atw_` + 32 random bytes (URL-safe base64). Only the SHA-256 of a key is stored.

A key is high-entropy, so a plain SHA-256 is enough to make a leaked `api_keys` collection
useless; a slow password hash would only add latency to every request.
"""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Sequence
from datetime import datetime
from typing import Protocol

from beanie import PydanticObjectId
from pydantic import BaseModel

from app.db.documents import ApiKey, Scope, utcnow

KEY_PREFIX = "atw_"
SCOPES: tuple[Scope, ...] = ("admin", "public:read", "leads:write")


class KeyRecord(BaseModel):
    id: str
    name: str
    scopes: list[Scope]
    created_at: datetime
    revoked_at: datetime | None = None
    last_used_at: datetime | None = None

    @property
    def active(self) -> bool:
        return self.revoked_at is None


class ApiKeyStore(Protocol):
    async def add(self, name: str, key_hash: str, scopes: Sequence[Scope]) -> KeyRecord: ...

    async def find_by_hash(self, key_hash: str) -> KeyRecord | None: ...

    async def touch(self, key_id: str, at: datetime) -> None: ...

    async def list(self) -> list[KeyRecord]: ...

    async def revoke(self, name: str, at: datetime) -> int: ...


def generate_key() -> str:
    return KEY_PREFIX + secrets.token_urlsafe(32)


def hash_key(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode()).hexdigest()


async def issue_key(
    store: ApiKeyStore, name: str, scopes: Sequence[Scope]
) -> tuple[KeyRecord, str]:
    """Create a key. The plaintext is returned once and never stored."""
    unknown = set(scopes) - set(SCOPES)
    if unknown or not scopes:
        raise ValueError(f"scopes must be a non-empty subset of {', '.join(SCOPES)}")
    plaintext = generate_key()
    record = await store.add(name, hash_key(plaintext), list(dict.fromkeys(scopes)))
    return record, plaintext


def _record(doc: ApiKey) -> KeyRecord:
    return KeyRecord(
        id=str(doc.id),
        name=doc.name,
        scopes=doc.scopes,
        created_at=doc.created_at,
        revoked_at=doc.revoked_at,
        last_used_at=doc.last_used_at,
    )


class MongoApiKeyStore:
    async def add(self, name: str, key_hash: str, scopes: Sequence[Scope]) -> KeyRecord:
        doc = ApiKey(name=name, key_hash=key_hash, scopes=list(scopes))
        await doc.insert()
        return _record(doc)

    async def find_by_hash(self, key_hash: str) -> KeyRecord | None:
        doc = await ApiKey.find_one(ApiKey.key_hash == key_hash)
        return _record(doc) if doc else None

    async def touch(self, key_id: str, at: datetime) -> None:
        await ApiKey.get_pymongo_collection().update_one(
            {"_id": PydanticObjectId(key_id)}, {"$set": {"last_used_at": at}}
        )

    async def list(self) -> list[KeyRecord]:
        return [_record(doc) for doc in await ApiKey.find_all().sort("+created_at").to_list()]

    async def revoke(self, name: str, at: datetime | None = None) -> int:
        result = await ApiKey.get_pymongo_collection().update_many(
            {"name": name, "revoked_at": None}, {"$set": {"revoked_at": at or utcnow()}}
        )
        return result.modified_count
