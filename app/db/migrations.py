"""Versioned data migrations, applied in order at startup, each exactly once.

A migration is a module in `app/migrations/` named `m<version>_<slug>.py` that defines
`DESCRIPTION: str` and `async def up(db) -> None`. `up` must be idempotent: a crash between
running it and writing its marker means it runs again on the next start. Index changes do not
belong here; declare them on the document class instead.
"""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from types import ModuleType
from typing import Any

import structlog
from pymongo.asynchronous.database import AsyncDatabase

from app.db.documents import AppliedMigration

logger = structlog.get_logger(__name__)

Database = AsyncDatabase[dict[str, Any]]


@dataclass(frozen=True)
class Migration:
    version: str
    description: str
    up: Callable[[Database], Awaitable[None]]


def _load(module: ModuleType) -> Migration:
    version = module.__name__.rsplit(".", 1)[-1].split("_", 1)[0].removeprefix("m")
    return Migration(version=version, description=module.DESCRIPTION, up=module.up)


def discover(package: str = "app.migrations") -> list[Migration]:
    pkg = importlib.import_module(package)
    modules = [
        importlib.import_module(f"{package}.{info.name}")
        for info in pkgutil.iter_modules(pkg.__path__)
        if info.name.startswith("m")
    ]
    return sorted((_load(m) for m in modules), key=lambda m: m.version)


async def run_migrations(db: Database, migrations: list[Migration] | None = None) -> list[str]:
    """Apply every migration without a marker. Returns the versions applied this run."""
    pending = migrations if migrations is not None else discover()
    applied: list[str] = []
    for migration in pending:
        if await AppliedMigration.get(migration.version) is not None:
            continue
        await migration.up(db)
        await AppliedMigration(id=migration.version, description=migration.description).insert()
        logger.info("migration applied", version=migration.version)
        applied.append(migration.version)
    return applied
