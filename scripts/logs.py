"""Show container logs: `python -m scripts.logs [service] [--follow] [--tail N]`."""

from __future__ import annotations

import argparse
import contextlib

from scripts._common import compose


def build_args(service: str | None, follow: bool, tail: int) -> list[str]:
    args = ["logs", "--tail", str(tail)]
    if follow:
        args.append("--follow")
    if service:
        args.append(service)
    return args


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("service", nargs="?", help="mongodb | valkey | qdrant (default: all)")
    parser.add_argument("-f", "--follow", action="store_true")
    parser.add_argument("--tail", type=int, default=100)
    args = parser.parse_args(argv)
    with contextlib.suppress(KeyboardInterrupt):
        compose(*build_args(args.service, args.follow, args.tail), check=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
