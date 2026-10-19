"""A LangGraph checkpointer on our PyMongo async client, for the intake assistant.

`langgraph-checkpoint-mongodb` (0.5.x) only drives a synchronous `MongoClient` and runs every
async call in a thread pool, and it brings `langchain-mongodb` with numpy along. This saver
implements the async half of `BaseCheckpointSaver` directly on the `checkpoints` and
`checkpoint_writes` collections (declared in `app.db.documents`). The graph is only ever run
asynchronously, so the sync methods are left to the base class, which refuses them.

Checkpoints are keyed by conversation id (`thread_id`), so a pending confirmation survives an API
restart, a Redis flush, and a vault export/import to another machine. `prune_finished` drops
threads idle for longer than the retention period unless they still wait on a confirmation.
"""

from __future__ import annotations

import random
import re
from collections.abc import AsyncIterator, Sequence
from datetime import datetime
from typing import Any, cast

import structlog
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    WRITES_IDX_MAP,
    BaseCheckpointSaver,
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
    get_checkpoint_id,
    get_checkpoint_metadata,
)
from langgraph.checkpoint.serde.base import SerializerProtocol
from langgraph.checkpoint.serde.types import INTERRUPT
from pymongo import ASCENDING, DESCENDING
from pymongo.asynchronous.collection import AsyncCollection

from app.db.documents import CheckpointEntry, CheckpointWrite, utcnow

logger = structlog.get_logger(__name__)

Collection = AsyncCollection[dict[str, Any]]


def _ids(config: RunnableConfig) -> tuple[str, str]:
    configurable = config["configurable"]
    return str(configurable["thread_id"]), str(configurable.get("checkpoint_ns", ""))


def _config(thread_id: str, ns: str, checkpoint_id: str) -> RunnableConfig:
    return {
        "configurable": {
            "thread_id": thread_id,
            "checkpoint_ns": ns,
            "checkpoint_id": checkpoint_id,
        }
    }


class MongoCheckpointSaver(BaseCheckpointSaver[str]):
    def __init__(
        self,
        checkpoints: Collection | None = None,
        writes: Collection | None = None,
        *,
        serde: SerializerProtocol | None = None,
    ) -> None:
        super().__init__(serde=serde)
        self._checkpoints = checkpoints
        self._writes = writes

    # Collections are looked up lazily: Beanie binds them at startup, after this is built.
    @property
    def checkpoints(self) -> Collection:
        return self._checkpoints or CheckpointEntry.get_pymongo_collection()

    @property
    def writes(self) -> Collection:
        return self._writes or CheckpointWrite.get_pymongo_collection()

    # --- read -------------------------------------------------------------------------------

    async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        thread_id, ns = _ids(config)
        query: dict[str, Any] = {"thread_id": thread_id, "checkpoint_ns": ns}
        if checkpoint_id := get_checkpoint_id(config):
            query["checkpoint_id"] = checkpoint_id
        doc = await self.checkpoints.find_one(query, sort=[("checkpoint_id", DESCENDING)])
        if doc is None:
            return None
        return await self._tuple(doc)

    async def alist(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
        query: dict[str, Any] = {}
        if config is not None:
            thread_id, ns = _ids(config)
            query |= {"thread_id": thread_id, "checkpoint_ns": ns}
            if checkpoint_id := get_checkpoint_id(config):
                query["checkpoint_id"] = checkpoint_id
        if before is not None and (before_id := get_checkpoint_id(before)):
            query["checkpoint_id"] = {"$lt": before_id}
        cursor = self.checkpoints.find(query, sort=[("checkpoint_id", DESCENDING)])
        returned = 0
        async for doc in cursor:
            item = await self._tuple(doc)
            if filter and not all(item.metadata.get(k) == v for k, v in filter.items()):
                continue
            yield item
            returned += 1
            if limit is not None and returned >= limit:
                break

    async def _tuple(self, doc: dict[str, Any]) -> CheckpointTuple:
        thread_id, ns, checkpoint_id = doc["thread_id"], doc["checkpoint_ns"], doc["checkpoint_id"]
        cursor = self.writes.find(
            {"thread_id": thread_id, "checkpoint_ns": ns, "checkpoint_id": checkpoint_id}
        )
        rows = sorted(
            [row async for row in cursor],
            key=lambda r: (r.get("task_path", ""), r["task_id"], r["idx"]),
        )
        parent = doc.get("parent_checkpoint_id")
        return CheckpointTuple(
            config=_config(thread_id, ns, checkpoint_id),
            checkpoint=self.serde.loads_typed((doc["checkpoint_type"], doc["checkpoint"])),
            metadata=self.serde.loads_typed((doc["metadata_type"], doc["metadata"])),
            parent_config=_config(thread_id, ns, parent) if parent else None,
            pending_writes=[
                (r["task_id"], r["channel"], self.serde.loads_typed((r["value_type"], r["value"])))
                for r in rows
            ],
        )

    # --- write ------------------------------------------------------------------------------

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        thread_id, ns = _ids(config)
        checkpoint_type, checkpoint_bytes = self.serde.dumps_typed(checkpoint)
        metadata_type, metadata_bytes = self.serde.dumps_typed(
            get_checkpoint_metadata(config, metadata)
        )
        key = {"thread_id": thread_id, "checkpoint_ns": ns, "checkpoint_id": checkpoint["id"]}
        await self.checkpoints.update_one(
            key,
            {
                "$set": {
                    "parent_checkpoint_id": config["configurable"].get("checkpoint_id"),
                    "checkpoint_type": checkpoint_type,
                    "checkpoint": checkpoint_bytes,
                    "metadata_type": metadata_type,
                    "metadata": metadata_bytes,
                    "updated_at": utcnow(),
                }
            },
            upsert=True,
        )
        return _config(thread_id, ns, checkpoint["id"])

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        thread_id, ns = _ids(config)
        checkpoint_id = str(config["configurable"]["checkpoint_id"])
        now = utcnow()
        for index, (channel, value) in enumerate(writes):
            idx = WRITES_IDX_MAP.get(channel, index)
            value_type, value_bytes = self.serde.dumps_typed(value)
            key = {
                "thread_id": thread_id,
                "checkpoint_ns": ns,
                "checkpoint_id": checkpoint_id,
                "task_id": task_id,
                "idx": idx,
            }
            fields = {
                "channel": channel,
                "value_type": value_type,
                "value": value_bytes,
                "task_path": task_path,
                "updated_at": now,
            }
            # A task's own output is written once; special channels (interrupt, resume, error)
            # replace the previous value, as LangGraph's in-memory saver does.
            operator = "$setOnInsert" if idx >= 0 else "$set"
            await self.writes.update_one(key, {operator: fields}, upsert=True)

    async def adelete_thread(self, thread_id: str) -> None:
        await self.checkpoints.delete_many({"thread_id": thread_id})
        await self.writes.delete_many({"thread_id": thread_id})

    def get_next_version(self, current: str | None, channel: None) -> str:
        """Monotonic string versions, as LangGraph's own savers use."""
        current_v = 0 if current is None else int(str(current).split(".")[0])
        return f"{current_v + 1:032}.{random.random():016}"

    # --- housekeeping -----------------------------------------------------------------------

    async def waiting(self, thread_id: str) -> bool:
        """True when the thread's latest checkpoint holds an unanswered interrupt."""
        latest = await self.aget_tuple({"configurable": {"thread_id": thread_id}})
        return latest is not None and any(w[1] == INTERRUPT for w in latest.pending_writes or [])

    async def prune_finished(
        self, older_than: datetime, *, prefix: str | None = None, keep_waiting: bool = True
    ) -> int:
        """Delete threads last touched before `older_than` that wait on nothing (or, with
        `keep_waiting=False`, whatever they wait on). `prefix` limits it to thread ids starting
        with it (`public:`). Returns how many threads were deleted."""
        match: list[dict[str, Any]] = (
            [{"$match": {"thread_id": {"$regex": f"^{re.escape(prefix)}"}}}] if prefix else []
        )
        pipeline: list[dict[str, Any]] = [
            *match,
            {"$group": {"_id": "$thread_id", "last": {"$max": "$updated_at"}}},
            {"$match": {"last": {"$lt": older_than}}},
            {"$sort": {"last": ASCENDING}},
        ]
        stale = [row["_id"] async for row in await self.checkpoints.aggregate(pipeline)]
        deleted = 0
        for thread_id in stale:
            if keep_waiting and await self.waiting(cast(str, thread_id)):
                continue
            await self.adelete_thread(thread_id)
            deleted += 1
        logger.info("checkpoints pruned", threads=deleted, kept=len(stale) - deleted)
        return deleted
