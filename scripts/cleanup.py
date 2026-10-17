"""Remove local containers, data volumes and tool caches: `python -m scripts.cleanup [--apply]`.

Dry run by default: it lists what would go. `--apply` deletes it. `--keep-data` keeps the
volumes (MongoDB, Valkey, Qdrant). Export first if the vault holds real data.
"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

from scripts._common import ROOT, compose, info, ok, paint, warn

CACHE_DIRS = (".pytest_cache", ".ruff_cache", ".mypy_cache", "htmlcov")
CACHE_FILES = (".coverage", "coverage.xml")


def local_caches(root: Path = ROOT) -> list[Path]:
    found = [root / name for name in (*CACHE_DIRS, *CACHE_FILES) if (root / name).exists()]
    for folder in ("app", "scripts", "tests"):
        base = root / folder
        if base.exists():
            found.extend(sorted(base.rglob("__pycache__")))
    return found


def plan(keep_data: bool, root: Path = ROOT) -> tuple[list[str], list[Path]]:
    compose_args = (
        ["down", "--remove-orphans"] if keep_data else ["down", "--remove-orphans", "--volumes"]
    )
    return compose_args, local_caches(root)


def remove(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)
    else:
        path.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="actually delete")
    parser.add_argument("--keep-data", action="store_true", help="keep the data volumes")
    args = parser.parse_args(argv)

    compose_args, caches = plan(args.keep_data)
    data_note = "kept" if args.keep_data else paint("DELETED (MongoDB, Valkey, Qdrant data)", "red")
    info(f"Containers: stopped and removed; volumes: {data_note}")
    info(f"Local caches: {len(caches)} path(s)")
    for path in caches:
        print(f"    {path.relative_to(ROOT)}")

    if not args.apply:
        warn("Dry run, nothing changed. Re-run with --apply to delete.")
        return 0

    compose(*compose_args, check=False)
    for path in caches:
        remove(path)
    ok("Cleanup done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
