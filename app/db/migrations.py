"""Versioned data migrations, applied in order at startup, each exactly once.

A migration is a module in `app/migrations/` named `m<version>_<slug>.py` that defines
`DESCRIPTION: str` and `async def up(db) -> None`. `up` must be idempotent: a crash between
running it and writing its marker means it runs again on the next start. Index changes do not
belong here; declare them on the document class instead.

A migration may also define `async def down(db) -> None` that undoes exactly what `up` did. One
without `down` is irreversible (for example, a backfill whose fields now hold real data), and a
rollback past it is refused before anything changes. `scripts/migrate.py` runs both by hand.
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
    down: Callable[[Database], Awaitable[None]] | None = None


def _load(module: ModuleType) -> Migration:
    version = module.__name__.rsplit(".", 1)[-1].split("_", 1)[0].removeprefix("m")
    return Migration(
        version=version,
        description=module.DESCRIPTION,
        up=module.up,
        down=getattr(module, "down", None),
    )


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


class IrreversibleMigrationError(Exception):
    """A rollback would pass a migration that has no `down`."""

    def __init__(self, versions: list[str]) -> None:
        super().__init__(f"No down() for migration(s) {', '.join(versions)}; cannot roll back")
        self.versions = versions


async def applied_versions() -> set[str]:
    return {m.id for m in await AppliedMigration.find_all().to_list()}


async def rollback_plan(target: str, migrations: list[Migration] | None = None) -> list[Migration]:
    """Applied migrations newer than `target`, newest first. Refuses (before any change) when
    one of them cannot be undone. `target` "0000" means roll back everything."""
    known = migrations if migrations is not None else discover()
    applied = await applied_versions()
    plan = sorted(
        (m for m in known if m.version > target and m.version in applied),
        key=lambda m: m.version,
        reverse=True,
    )
    blocked = [m.version for m in plan if m.down is None]
    if blocked:
        raise IrreversibleMigrationError(blocked)
    return plan


async def rollback(db: Database, plan: list[Migration]) -> list[str]:
    """Run `down` for each planned migration and remove its marker. Returns the versions."""
    undone: list[str] = []
    for migration in plan:
        assert migration.down is not None
        await migration.down(db)
        marker = await AppliedMigration.get(migration.version)
        if marker is not None:
            await marker.delete()
        logger.info("migration rolled back", version=migration.version)
        undone.append(migration.version)
    return undone
