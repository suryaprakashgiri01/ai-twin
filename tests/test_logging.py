from __future__ import annotations

import json
import logging

import pytest
import structlog

from app.core import logging as app_logging


def test_auto_format_follows_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    assert app_logging.resolve_log_format("auto") == "console"
    monkeypatch.setattr("sys.stdout.isatty", lambda: False)
    assert app_logging.resolve_log_format("auto") == "json"
    assert app_logging.resolve_log_format("console") == "console"


def test_console_renderer_is_coloured_on_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    renderer = app_logging.build_renderer("console")
    assert isinstance(renderer, structlog.dev.ConsoleRenderer)
    assert "\x1b[" in renderer(None, "info", {"event": "hello", "level": "info"})


def test_json_lines_redact_secrets_and_carry_context(capsys: pytest.CaptureFixture[str]) -> None:
    app_logging.setup_logging(level="INFO", log_format="json")
    structlog.contextvars.bind_contextvars(request_id="r-1")
    try:
        app_logging.get_logger("test").info("event", password="p", token="t", keep="yes")
    finally:
        structlog.contextvars.clear_contextvars()
    line = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert line["password"] == "[REDACTED]"
    assert line["token"] == "[REDACTED]"
    assert line["keep"] == "yes"
    assert line["request_id"] == "r-1"
    assert "trace_id" not in line


def test_stdlib_loggers_share_the_format(capsys: pytest.CaptureFixture[str]) -> None:
    app_logging.setup_logging(level="INFO", log_format="json")
    logging.getLogger("uvicorn.error").info("from uvicorn")
    line = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert line["event"] == "from uvicorn"
    assert line["logger"] == "uvicorn.error"
