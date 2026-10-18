import pytest

from app.core.config import Settings
from app.core.telemetry import setup_telemetry
from tests.observability.fakes import captured

__all__ = ["captured"]


@pytest.fixture(autouse=True)
def request_spans() -> None:
    """The app's tracer provider, as create_app installs it: spans have valid trace ids."""
    setup_telemetry(Settings(_env_file=None))
