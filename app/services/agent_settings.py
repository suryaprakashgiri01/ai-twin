"""The owner's per-agent model settings: what each agent uses, and console overrides of it.

`config/agents.yaml` is the base. An override (`agent_model_overrides`, one row per agent)
replaces that agent's settings and is checked exactly like the file: an unknown model id, or a
cloud model for confidential steps, is refused. Rows are applied at startup and on every PUT or
DELETE, so the next call uses them.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

import structlog
from pydantic import ConfigDict

from app.agents.models import (
    AGENT_NAMES,
    AgentModel,
    AgentModelError,
    AgentModels,
    AgentName,
    SettingsSource,
)
from app.api.envelope import ApiModel
from app.db.documents import AgentModelOverride, utcnow
from app.exceptions import AppError, ErrorCode
from llm_orchestrator import Prefer
from llm_orchestrator.gateway import Complexity

logger = structlog.get_logger(__name__)

# What the console shows for each agent: a line about it and the route that runs it.
AGENT_INFO: dict[AgentName, tuple[str, str]] = {
    "owner": ("Ask: answers from your knowledge base, records and profile", "/admin/v1/chat"),
    "prep": ("Prep: interview and application answers, filtered for outward use", "/admin/v1/prep"),
    "intake": (
        "Intake: turns a message into records and notes, after a yes",
        "/admin/v1/assistant",
    ),
    "super": ("Super: plans a request and runs intake, ask and prep in turn", "/admin/v1/super"),
}


class AgentSettingsIn(ApiModel):
    model_config = ConfigDict(extra="forbid")

    model: str = "auto"
    prefer: Prefer | None = None
    confidential_model: str = "auto"
    confidential_prefer: Prefer | None = None
    need: Complexity | None = None

    def settings(self) -> AgentModel:
        return AgentModel(**self.model_dump())


class AgentSettings(AgentSettingsIn):
    name: AgentName
    description: str
    endpoint: str
    source: SettingsSource
    updated_at: datetime | None = None


class AgentOverrideStore(Protocol):
    async def list(self) -> list[tuple[str, AgentModel, datetime]]: ...

    async def put(self, name: str, settings: AgentModel) -> datetime: ...

    async def delete(self, name: str) -> bool: ...


class MongoAgentOverrideStore:
    async def list(self) -> list[tuple[str, AgentModel, datetime]]:
        rows = await AgentModelOverride.find_all().to_list()
        out = []
        for row in rows:
            try:
                fields = row.model_dump(exclude={"id", "revision_id", "updated_at"})
                out.append((row.id, AgentModel.model_validate(fields), row.updated_at))
            except ValueError:
                logger.warning("agent override skipped: no longer valid", agent=row.id)
        return out

    async def put(self, name: str, settings: AgentModel) -> datetime:
        at = utcnow()
        await AgentModelOverride(id=name, updated_at=at, **settings.model_dump()).save()
        return at

    async def delete(self, name: str) -> bool:
        row = await AgentModelOverride.get(name)
        if row is None:
            return False
        await row.delete()
        return True


class AgentSettingsService:
    def __init__(self, models: AgentModels, store: AgentOverrideStore) -> None:
        self._models = models
        self._store = store
        self._updated: dict[str, datetime] = {}

    async def load(self) -> None:
        """Apply stored overrides; one that no longer fits the inventory is skipped, loudly."""
        for name, settings, at in await self._store.list():
            if name not in AGENT_NAMES:
                continue
            agent: AgentName = name
            try:
                self._models.apply(agent, settings)
            except AgentModelError as exc:
                logger.warning("agent override skipped", agent=name, reason=str(exc))
                continue
            self._updated[name] = at

    def list(self) -> list[AgentSettings]:
        return [self.get(name) for name in AGENT_NAMES]

    def get(self, name: AgentName) -> AgentSettings:
        settings, source = self._models.settings(name)
        description, endpoint = AGENT_INFO[name]
        return AgentSettings(
            name=name,
            description=description,
            endpoint=endpoint,
            source=source,
            updated_at=self._updated.get(name) if source == "override" else None,
            **settings.model_dump(),
        )

    async def put(self, name: AgentName, body: AgentSettingsIn) -> AgentSettings:
        settings = body.settings()
        try:
            self._models.check(name, settings)
        except AgentModelError as exc:
            raise AppError(ErrorCode.INVALID_INPUT, str(exc)) from exc
        self._updated[name] = await self._store.put(name, settings)
        self._models.apply(name, settings)
        return self.get(name)

    async def reset(self, name: AgentName) -> AgentSettings:
        await self._store.delete(name)
        self._updated.pop(name, None)
        self._models.apply(name, None)
        return self.get(name)
