"""MongoDB data migrations by hand: `python -m scripts.migrate status|upgrade|downgrade`.

  status                       every migration: applied or pending, reversible or not
  upgrade                      apply pending migrations (the API also does this at every start)
  downgrade <version> [--apply]
                               roll back applied migrations newer than <version> (0000 = all);
                               a dry run unless --apply. Stop the API first: its next start
                               applies pending migrations again, so a downgrade only sticks while
                               you run code from before that migration.

There is no SQL database and no Alembic: these are versioned data changes on MongoDB
(`app/migrations/m<version>_<slug>.py`). Indexes come from the document classes at startup.
Talks to MongoDB using MONGODB_URI from `.env`; the stack must be up.
"""

from __future__ import annotations

import argparse
import asyncio
import re
from collections.abc import Sequence

from app.core.config import Settings
from app.core.logging import setup_logging
from app.db.client import MongoClient, init_mongo
from app.db.documents import DOCUMENT_MODELS
from app.db.migrations import (
    IrreversibleMigrationError,
    applied_versions,
    discover,
    rollback,
    rollback_plan,
    run_migrations,
)
from scripts._common import fail, info, ok, warn

VERSION = re.compile(r"^[0-9]{4}$")


def parse(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="list migrations and whether each is applied")
    sub.add_parser("upgrade", help="apply pending migrations")
    down = sub.add_parser("downgrade", help="roll back to a version (dry run without --apply)")
    down.add_argument("target", help="the version to keep, e.g. 0003 (0000 = roll back all)")
    down.add_argument("--apply", action="store_true", help="really roll back")
    return parser.parse_args(argv)


async def status() -> int:
    applied = await applied_versions()
    for m in discover():
        state = "applied" if m.version in applied else "pending"
        undo = "reversible" if m.down else "irreversible"
        print(f"{m.version}  {state:<8} {undo:<12} {m.description}")
    return 0


async def upgrade(settings: Settings, client: MongoClient) -> int:
    done = await run_migrations(client[settings.mongodb_database])
    if done:
        ok(f"applied {', '.join(done)}")
    else:
        info("nothing pending")
    return 0


async def downgrade(settings: Settings, client: MongoClient, target: str, apply: bool) -> int:
    if not VERSION.match(target):
        fail(f"version must be four digits, e.g. 0003 (got {target!r})")
        return 1
    try:
        plan = await rollback_plan(target)
    except IrreversibleMigrationError as exc:
        fail(str(exc))
        return 1
    if not plan:
        info(f"nothing applied after {target}")
        return 0
    for m in plan:
        print(f"roll back {m.version}  {m.description}")
    if not apply:
        warn("dry run: nothing changed. Stop the API, then add --apply")
        return 0
    undone = await rollback(client[settings.mongodb_database], plan)
    ok(f"rolled back {', '.join(undone)}")
    warn("the next API start applies pending migrations again")
    return 0


async def _run(args: argparse.Namespace) -> int:
    settings = Settings()
    client = await init_mongo(settings, DOCUMENT_MODELS)
    try:
        if args.command == "status":
            return await status()
        if args.command == "upgrade":
            return await upgrade(settings, client)
        return await downgrade(settings, client, args.target, args.apply)
    finally:
        await client.close()


def main(argv: Sequence[str] | None = None) -> int:
    args = parse(argv)
    setup_logging(level="WARNING", log_format="console")
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
