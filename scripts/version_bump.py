"""Release version bump for the develop -> main PR: `python -m scripts.version_bump step`.

  patch|minor|major   relative to the current version
  X.Y.Z               absolute version

Reads the `DEFAULT_VERSION` constant from `app.core.config` (single source of truth, the
service reports it from `/livez`), rewrites that line, prints the change and returns
non-zero when the version did not change (e.g. `patch` on an unreleased content) so a
release PR cannot be opened accidentally with an unversioned bump.
"""

from __future__ import annotations

import argparse
import re
import sys

from scripts._common import ROOT, fail, ok

CONFIG_PATH = ROOT / "app" / "core" / "config.py"
VERSION_LINE = re.compile(r'^(DEFAULT_VERSION(?::\s*str)?\s*=\s*)"(?P<v>\d+\.\d+\.\d+)"', re.M)

# n.b. the public service reports `settings.service_version`; its default references
# DEFAULT_VERSION, so bumping the constant bumps both.


def current_version() -> tuple[str, str]:
    """The (line, version) of `DEFAULT_VERSION = "x.y.z"` in app/core/config.py."""
    text = CONFIG_PATH.read_text(encoding="utf-8")
    match = VERSION_LINE.search(text)
    if match is None:
        raise RuntimeError(f"DEFAULT_VERSION not found in {CONFIG_PATH}")
    line = match.group(0)
    return line, match.group("v")


def bump(from_version: str, step: str) -> str:
    if step in {"patch", "minor", "major"}:
        major, minor, patch = (int(part) for part in from_version.split("."))
        if step == "patch":
            patch += 1
        elif step == "minor":
            minor, patch = minor + 1, 0
        else:
            major, minor, patch = major + 1, 0, 0
        return f"{major}.{minor}.{patch}"
    return step


def rewrite(new_version: str) -> bool:
    text = CONFIG_PATH.read_text(encoding="utf-8")
    match = VERSION_LINE.search(text)
    if match is None:
        fail(f"DEFAULT_VERSION not found in {CONFIG_PATH}")
        return False
    if match.group("v") == new_version:
        return False  # nothing changed
    text = text[: match.start()] + f'DEFAULT_VERSION = "{new_version}"' + text[match.end() :]
    CONFIG_PATH.write_text(encoding="utf-8", data=text)
    return True


def parse(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument(
        "step",
        choices=["patch", "minor", "major", "absolute"],
        help="patch|minor|major relative, or 'absolute' with X.Y.Z as the next arg",
    )
    parser.add_argument("version", nargs="?", help="X.Y.Z when step is 'absolute'")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse(argv)
    _, current = current_version()
    if args.step == "absolute":
        if args.version is None or not re.fullmatch(r"\d+\.\d+\.\d+", args.version):
            fail("usage: python -m scripts.version_bump absolute X.Y.Z")
            return 1
        target = args.version
    else:
        target = bump(current, args.step)
    changed = rewrite(target)
    if not changed:
        # Already at the requested version: a release PR must not open unversioned.
        fail(f"version unchanged: {CONFIG_PATH.name} already says {current}")
        return 1
    ok(f"version: {current} -> {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
