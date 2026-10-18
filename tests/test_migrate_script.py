"""scripts.migrate: argument parsing and the dry-run / refusal paths, without a database."""

from __future__ import annotations

from typing import Any

import pytest

from app.core.config import Settings
from app.db.migrations import IrreversibleMigrationError, Migration
from scripts import migrate


def test_parse_commands() -> None:
    assert migrate.parse(["status"]).command == "status"
    args = migrate.parse(["downgrade", "0003", "--apply"])
    assert (args.command, args.target, args.apply) == ("downgrade", "0003", True)
    assert migrate.parse(["downgrade", "0003"]).apply is False
    with pytest.raises(SystemExit):
        migrate.parse(["sideways"])


async def _noop(db: Any) -> None:
    return None


async def test_downgrade_refuses_bad_versions_and_irreversible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(_env_file=None)
    client: Any = None
    assert await migrate.downgrade(settings, client, "3", apply=True) == 1

    async def blocked(target: str) -> list[Migration]:
        raise IrreversibleMigrationError(["0003"])

    monkeypatch.setattr(migrate, "rollback_plan", blocked)
    assert await migrate.downgrade(settings, client, "0002", apply=True) == 1


async def test_downgrade_is_a_dry_run_without_apply(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    async def plan(target: str) -> list[Migration]:
        return [Migration("0004", "mode", _noop, _noop)]

    async def rollback(db: Any, planned: list[Migration]) -> list[str]:
        calls.append("rollback")
        return [m.version for m in planned]

    monkeypatch.setattr(migrate, "rollback_plan", plan)
    monkeypatch.setattr(migrate, "rollback", rollback)
    settings = Settings(_env_file=None)
    client: Any = {settings.mongodb_database: object()}
    assert await migrate.downgrade(settings, client, "0003", apply=False) == 0
    assert calls == []
    assert await migrate.downgrade(settings, client, "0003", apply=True) == 0
    assert calls == ["rollback"]
