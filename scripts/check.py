"""The quality gate, same on every OS: `python -m scripts.check`.

ruff lint + format check, mypy strict, pytest with the coverage gate. Stops at the first failure.
"""

from __future__ import annotations

import subprocess

from scripts._common import fail, info, ok, run

STEPS: tuple[tuple[str, list[str]], ...] = (
    ("lint", ["uv", "run", "ruff", "check", "."]),
    ("format", ["uv", "run", "ruff", "format", "--check", "."]),
    ("types", ["uv", "run", "mypy", "app", "scripts", "tests", "packages/llm-orchestrator/src"]),
    (
        "tests",
        [
            "uv",
            "run",
            "pytest",
            "-m",
            "not integration",
            "--cov",
            "--cov-report=term-missing:skip-covered",
        ],
    ),
)


def main() -> int:
    for name, argv in STEPS:
        info(f"{name}: {' '.join(argv[2:])}")
        try:
            run(argv)
        except subprocess.CalledProcessError:
            fail(f"{name} failed")
            return 1
    ok("All checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
