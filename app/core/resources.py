"""Process-wide storage handles, opened in the app lifespan and closed on shutdown."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager
from dataclasses import dataclass

from qdrant_client import AsyncQdrantClient
from redis.asyncio import Redis

from app.auth.keys import ApiKeyStore, MongoApiKeyStore
from app.cache.redis import open_redis
from app.core.config import Settings
from app.db.client import MongoClient, init_mongo
from app.db.documents import DOCUMENT_MODELS
from app.db.migrations import run_migrations
from app.kb.store import KbStore, MongoKbStore
from app.kb.vectors import QdrantIndex, VectorIndex
from app.leads.store import LeadStore, MongoLeadStore
from app.services.agent_settings import AgentOverrideStore, MongoAgentOverrideStore
from app.services.audit import AuditLog, MongoAuditLog
from app.services.conversations import ConversationService, MongoConversationStore
from app.services.model_overrides import MongoOverrideStore, OverrideStore
from app.services.profile import MongoProfileStore, MongoRecordStore, ProfileStore, RecordStore
from app.services.security_events import MongoSecurityEvents, SecurityEventWriter
from app.storage.blobs import BlobStore
from app.vector.qdrant import open_qdrant


@dataclass
class Resources:
    mongo: MongoClient
    redis: Redis
    qdrant: AsyncQdrantClient
    conversations: ConversationService
    blobs: BlobStore
    api_keys: ApiKeyStore
    overrides: OverrideStore
    agent_overrides: AgentOverrideStore
    audit: AuditLog
    kb: KbStore
    vectors: VectorIndex
    profile: ProfileStore
    records: RecordStore
    security_events: SecurityEventWriter
    leads: LeadStore


ResourcesFactory = Callable[[Settings], AbstractAsyncContextManager[Resources]]


@asynccontextmanager
async def open_resources(settings: Settings) -> AsyncIterator[Resources]:
    async with AsyncExitStack() as stack:
        mongo = await init_mongo(settings, DOCUMENT_MODELS)
        stack.push_async_callback(mongo.close)
        await run_migrations(mongo[settings.mongodb_database])

        redis = await open_redis(settings)
        stack.push_async_callback(redis.aclose)
        conversations = ConversationService(
            redis,
            MongoConversationStore(),
            visitor_salt=settings.visitor_hash_salt.get_secret_value(),
            cache_turns=settings.conversation_cache_turns,
            public_retention_days=settings.public_conversation_retention_days,
        )
        stack.push_async_callback(conversations.drain)

        qdrant = await open_qdrant(settings)
        stack.push_async_callback(qdrant.close)

        blobs = BlobStore(settings.blob_dir, settings.blob_encryption_key.get_secret_value())

        yield Resources(
            mongo=mongo,
            redis=redis,
            qdrant=qdrant,
            conversations=conversations,
            blobs=blobs,
            api_keys=MongoApiKeyStore(),
            overrides=MongoOverrideStore(),
            agent_overrides=MongoAgentOverrideStore(),
            audit=MongoAuditLog(),
            kb=MongoKbStore(),
            vectors=QdrantIndex(qdrant),
            profile=MongoProfileStore(),
            records=MongoRecordStore(),
            security_events=MongoSecurityEvents(),
            leads=MongoLeadStore(),
        )
