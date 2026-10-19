"""dev-tooling T2: `python -m app.main --reload` turns reload on without an environment variable."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from app import main
from app.core.config import get_settings

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(("argv", "expected"), [([], False), (["--reload"], True)])
def test_reload_flag(monkeypatch: pytest.MonkeyPatch, argv: list[str], expected: bool) -> None:
    seen: dict[str, Any] = {}
    monkeypatch.setattr("uvicorn.run", lambda *a, **kw: seen.update(kw))
    monkeypatch.delenv("RELOAD", raising=False)
    get_settings.cache_clear()
    main.run(argv)
    assert seen["reload"] is expected


def test_makefile_recipes_set_no_env_vars_inline() -> None:
    # `VAR=value cmd` is POSIX shell; GNU make on Windows runs recipes in cmd.exe.
    recipes = [
        line for line in (ROOT / "Makefile").read_text().splitlines() if line.startswith("\t")
    ]
    assert [r for r in recipes if re.match(r"\t@?[A-Z_]+=", r)] == []


def test_reload_watches_code_and_config_only(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}
    monkeypatch.setattr("uvicorn.run", lambda *a, **kw: seen.update(kw))
    get_settings.cache_clear()
    main.run(["--reload"])
    assert seen["reload_dirs"] == ["app", "packages", "config"]
    assert not {"vault", "graphify-out", "."} & set(seen["reload_dirs"])
