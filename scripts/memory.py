"""What the owner agents remember about you: `python -m scripts.memory <command>`.

  list [--pending | --archived]           active and pending items (or only one status)
  add "<text>" [--kind K] [--confidential] remember something now (active at once)
  accept <id>                              start using a learned (pending) item
  edit <id> "<text>" [--kind K]            change its words or kind
  forget <id>                              archive it: no longer used, kept for the record
  delete <id>                              remove it for good

Kinds: instruction (a standing rule), preference (how answers should be), alias (a name you
use). Learned items wait as pending until you accept them. Talks to the running API with the
admin key in AI_TWIN_ADMIN_KEY, like make chat. Every change is audited.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from scripts._common import fail, info, ok
from scripts.chat import Api, CliError, load_config, render_memories

KINDS = ("instruction", "preference", "alias")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--base-url", help="API address (default: http://HOST:PORT from .env)")
    sub = parser.add_subparsers(dest="command")
    listing = sub.add_parser("list", help="list memories")
    status = listing.add_mutually_exclusive_group()
    status.add_argument("--pending", action="store_true")
    status.add_argument("--archived", action="store_true")
    add = sub.add_parser("add", help="remember something now")
    add.add_argument("text")
    add.add_argument("--kind", choices=KINDS, default="preference")
    add.add_argument("--confidential", action="store_true", help="local-model calls only")
    for name in ("accept", "forget", "delete"):
        sub.add_parser(name).add_argument("id")
    edit = sub.add_parser("edit", help="change a memory's words or kind")
    edit.add_argument("id")
    edit.add_argument("text", nargs="?")
    edit.add_argument("--kind", choices=KINDS)
    return parser


def execute(args: argparse.Namespace, api: Api) -> int:
    command = args.command or "list"
    if command == "list":
        status = "pending" if args.pending else "archived" if args.archived else None
        items = api.memories(status)
        print(render_memories(items))
        pending = sum(1 for m in items if m.get("status") == "pending")
        if pending and status is None:
            info(f'{pending} waiting for review: make memory ARGS="accept <id>"')
        return 0
    if command == "add":
        tier = "confidential" if args.confidential else "private"
        item = api.remember(args.text, args.kind, tier)
        ok(f"Remembered {item['id']} ({item['kind']}, {item['status']})")
    elif command == "accept":
        item = api.update_memory(args.id, status="active")
        ok(f"Accepted {item['id']}: used from the next message")
    elif command == "edit":
        changes = {k: v for k, v in (("text", args.text), ("kind", args.kind)) if v}
        if not changes:
            fail("Give new text and/or --kind")
            return 1
        item = api.update_memory(args.id, **changes)
        ok(f"Edited {item['id']}: {item['text']} ({item['kind']})")
    elif command == "forget":
        api.update_memory(args.id, status="archived")
        ok(f'Forgotten {args.id} (archived; make memory ARGS="list --archived")')
    elif command == "delete":
        api.delete_memory(args.id)
        ok(f"Deleted {args.id}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        api = Api(load_config(args.base_url))
    except CliError as exc:
        fail(str(exc))
        return 1
    try:
        return execute(args, api)
    except CliError as exc:
        fail(str(exc))
        return 1
    finally:
        api.close()


if __name__ == "__main__":
    sys.exit(main())
