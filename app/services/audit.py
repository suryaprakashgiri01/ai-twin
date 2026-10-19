"""Audit log: who changed what, through the admin API. Append-only."""

from __future__ import annotations

from typing import Any, Protocol

import structlog

from app.db.documents import AuditEntry

logger = structlog.get_logger(__name__)


class AuditLog(Protocol):
    async def record(
        self, actor: str, action: str, target: str, details: dict[str, Any]
    ) -> None: ...


class MongoAuditLog:
    async def record(self, actor: str, action: str, target: str, details: dict[str, Any]) -> None:
        await AuditEntry(actor=actor, action=action, target=target, details=details).insert()
        logger.info("audit", actor=actor, action=action, target=target)
