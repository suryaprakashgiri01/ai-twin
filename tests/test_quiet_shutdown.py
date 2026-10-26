"""Windows `make run`: nothing is printed after Ctrl+C starts the shutdown."""

from __future__ import annotations

import logging

from app.core.logging import QuietAfterShutdown, quiet_shutdown_wanted


def record(name: str, message: str) -> logging.LogRecord:
    return logging.LogRecord(name, logging.INFO, __file__, 1, message, None, None)


def test_lines_stop_once_uvicorn_starts_shutting_down() -> None:
    quiet = QuietAfterShutdown()
    assert quiet.filter(record("app.access", "request completed"))
    assert quiet.filter(record("uvicorn.error", "Application startup complete."))
    assert not quiet.filter(record("uvicorn.error", "Shutting down"))
    assert not quiet.filter(record("app.core.app_factory", "service stopped"))
    reloader = QuietAfterShutdown()
    assert not reloader.filter(record("uvicorn.error", "Stopping reloader process [27584]"))


def test_only_windows_under_make() -> None:
    assert quiet_shutdown_wanted({"MAKELEVEL": "1"}, "nt")
    assert not quiet_shutdown_wanted({}, "nt")
    assert not quiet_shutdown_wanted({"MAKELEVEL": "1"}, "posix")
