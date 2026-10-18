from __future__ import annotations

from collections.abc import Callable, Iterator, MutableMapping
from pathlib import Path
from typing import Any

import pytest
import structlog
from structlog.testing import capture_logs

from app.agents.graphs import intake as intake_graph
from app.agents.intake import confirm
from app.services import intake as intake_service
from tests.ingestion.conftest import Harness, blank_detector, harness
from tests.intake.harness import Twin

__all__ = ["blank_detector", "harness"]


@pytest.fixture
def make_twin(harness: Harness, tmp_path: Path) -> Callable[..., Twin]:
    def make(local: list[Any], **kwargs: Any) -> Twin:
        return Twin(harness, tmp_path, local, **kwargs)

    return make


@pytest.fixture
def intake_logs(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[MutableMapping[str, Any]]]:
    """Every log line from the intake graph, service and confirmation (see tests/agent)."""
    with capture_logs() as logs:
        for module in (intake_graph, intake_service, confirm):
            monkeypatch.setattr(module, "logger", structlog.get_logger(module.__name__))
        yield logs
