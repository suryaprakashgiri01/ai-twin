from __future__ import annotations

from collections.abc import Iterator, MutableMapping
from typing import Any

import pytest
import structlog
from structlog.testing import capture_logs

from app.agents.graphs import owner


@pytest.fixture
def owner_logs(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[MutableMapping[str, Any]]]:
    """Log lines from the owner graph. The app caches loggers on first use, so a logger that
    ran under another test's app config would bypass `capture_logs`; swap in a fresh one."""
    with capture_logs() as logs:
        monkeypatch.setattr(owner, "logger", structlog.get_logger(owner.__name__))
        yield logs
