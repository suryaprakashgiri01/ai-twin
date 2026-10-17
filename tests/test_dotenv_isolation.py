"""Libraries that call `load_dotenv()` on import must not copy `.env` into the process env."""

from __future__ import annotations

import os
from pathlib import Path

import dotenv
import pytest

import app  # noqa: F401  (sets the switch)
from app.core.config import Settings


def test_library_load_dotenv_is_a_no_op(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / ".env").write_text("GROQ_API_KEY=leaked-from-dotenv\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    # What magika does at import time.
    dotenv.load_dotenv(dotenv.find_dotenv(usecwd=True))
    assert "GROQ_API_KEY" not in os.environ


def test_settings_still_read_the_env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = tmp_path / ".env"
    env.write_text("GROQ_API_KEY=from-file\n", encoding="utf-8")
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    key = Settings(_env_file=env).groq_api_key
    assert key is not None and key.get_secret_value() == "from-file"
