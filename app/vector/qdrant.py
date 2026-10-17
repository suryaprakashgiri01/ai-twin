"""Qdrant client and idempotent collection bootstrap.

One collection per tier, so a public query can never reach private vectors by a filter mistake.
Each holds a named dense vector (`dense`, cosine) and a named sparse vector (`sparse`, IDF) for
hybrid search, plus payload indexes on the fields every query filters by.
"""

from __future__ import annotations

import warnings
from typing import Literal

import structlog
from qdrant_client import AsyncQdrantClient, models

from app.core.config import Settings
from app.db.client import StorageStartupError

logger = structlog.get_logger(__name__)

DENSE = "dense"
SPARSE = "sparse"

COLLECTIONS: dict[Literal["vault", "public"], tuple[str, ...]] = {
    "vault": ("kb_public", "kb_private", "kb_confidential"),
    # The public deployment never holds anything but published chunks.
    "public": ("kb_public",),
}

PAYLOAD_INDEXES: dict[str, models.PayloadSchemaType] = {
    "tier": models.PayloadSchemaType.KEYWORD,
    "document_id": models.PayloadSchemaType.KEYWORD,
    "category": models.PayloadSchemaType.KEYWORD,
    "published": models.PayloadSchemaType.BOOL,
}


def create_qdrant(settings: Settings) -> AsyncQdrantClient:
    api_key = settings.qdrant_api_key.get_secret_value() or None
    with warnings.catch_warnings():
        # The local stack is plain HTTP on 127.0.0.1; the client warns about any key over HTTP.
        warnings.filterwarnings("ignore", message="Api key is used with an insecure connection")
        # No version probe: it is a blocking request at construction, and the image is pinned.
        return AsyncQdrantClient(
            url=settings.qdrant_url, api_key=api_key, timeout=10, check_compatibility=False
        )


def _dense_size(info: models.CollectionInfo) -> int | None:
    vectors = info.config.params.vectors
    if isinstance(vectors, dict) and DENSE in vectors:
        return vectors[DENSE].size
    return None


async def _ensure_collection(client: AsyncQdrantClient, name: str, dim: int) -> bool:
    """Create the collection if missing, else check its shape. Returns True when created."""
    if not await client.collection_exists(name):
        await client.create_collection(
            collection_name=name,
            vectors_config={DENSE: models.VectorParams(size=dim, distance=models.Distance.COSINE)},
            sparse_vectors_config={SPARSE: models.SparseVectorParams(modifier=models.Modifier.IDF)},
        )
        return True
    size = _dense_size(await client.get_collection(name))
    if size != dim:
        logger.error("qdrant collection has the wrong shape", collection=name, dim=size, want=dim)
        raise StorageStartupError(f"Qdrant collection {name} has dense size {size}, want {dim}")
    return False


async def _ensure_payload_indexes(client: AsyncQdrantClient, name: str) -> list[str]:
    existing = (await client.get_collection(name)).payload_schema or {}
    added: list[str] = []
    for field, schema in PAYLOAD_INDEXES.items():
        if field in existing:
            continue
        await client.create_payload_index(name, field_name=field, field_schema=schema, wait=True)
        added.append(field)
    return added


async def ensure_collections(
    client: AsyncQdrantClient, profile: Literal["vault", "public"], dim: int
) -> list[str]:
    """Bring the profile's collections to the declared shape; returns what changed."""
    changes: list[str] = []
    for name in COLLECTIONS[profile]:
        if await _ensure_collection(client, name, dim):
            changes.append(name)
        changes += [f"{name}.{field}" for field in await _ensure_payload_indexes(client, name)]
    logger.info("qdrant ready", profile=profile, changes=changes)
    return changes


async def open_qdrant(settings: Settings) -> AsyncQdrantClient:
    client = create_qdrant(settings)
    try:
        await ensure_collections(client, settings.deployment_profile, settings.embedding_dim)
    except StorageStartupError:
        await client.close()
        raise
    except Exception as exc:
        await client.close()
        logger.error("qdrant bootstrap failed", error_type=type(exc).__name__)
        raise StorageStartupError("Qdrant unreachable or rejected the API key") from exc
    return client
