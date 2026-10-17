"""Stop the local stack. Data volumes are kept: `python -m scripts.down`."""

from __future__ import annotations

from scripts._common import compose, ok


def main() -> int:
    compose("down", "--remove-orphans")
    ok("Stack stopped (volumes kept; `cleanup --apply` removes them)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
