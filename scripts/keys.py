"""Manage admin API keys: `python -m scripts.keys create|list|revoke`.

  create NAME --scope admin [--scope leads:write]   prints the key ONCE; only its hash is stored
  list                                              names, scopes, state (never the key)
  revoke NAME                                       revokes every active key with that name

Talks to MongoDB using MONGODB_URI from `.env`; the stack must be up.
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Sequence

from app.auth.keys import SCOPES, ApiKeyStore, MongoApiKeyStore, issue_key
from app.core.config import Settings
from app.core.logging import setup_logging
from app.db.client import init_mongo
from app.db.documents import ApiKey, Scope, utcnow
from scripts._common import fail, info, ok, paint, warn


def parse(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create", help="create a key and print it once")
    create.add_argument("name")
    create.add_argument("--scope", action="append", choices=SCOPES, required=True, dest="scopes")
    sub.add_parser("list", help="list keys (never the key itself)")
    revoke = sub.add_parser("revoke", help="revoke every active key with this name")
    revoke.add_argument("name")
    return parser.parse_args(argv)


async def execute(args: argparse.Namespace, store: ApiKeyStore) -> int:
    if args.command == "create":
        scopes: list[Scope] = args.scopes
        record, plaintext = await issue_key(store, args.name, scopes)
        ok(f"created key '{record.name}' with scopes {', '.join(record.scopes)}")
        warn("Copy it now; it is not stored and cannot be shown again:")
        print(f"\n    {paint(plaintext, 'bold')}\n")
        return 0
    if args.command == "list":
        records = await store.list()
        if not records:
            info('No keys. Create one: make keys ARGS="create owner --scope admin"')
        for r in records:
            state = "active" if r.active else f"revoked {r.revoked_at:%Y-%m-%d}"
            used = f"{r.last_used_at:%Y-%m-%d %H:%M}" if r.last_used_at else "never"
            print(f"{r.name:<20} {','.join(r.scopes):<28} {state:<20} last used {used}")
        return 0
    revoked = await store.revoke(args.name, utcnow())
    if revoked:
        ok(f"revoked {revoked} key(s) named '{args.name}'")
        return 0
    fail(f"no active key named '{args.name}'")
    return 1


async def _run(args: argparse.Namespace) -> int:
    settings = Settings()
    client = await init_mongo(settings, [ApiKey])
    try:
        return await execute(args, MongoApiKeyStore())
    finally:
        await client.close()


def main(argv: Sequence[str] | None = None) -> int:
    args = parse(argv)
    setup_logging(level="WARNING", log_format="console")
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
