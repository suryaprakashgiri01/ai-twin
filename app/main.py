"""Process entrypoint: ``python -m app.main``."""

from __future__ import annotations

import uvicorn

from app.core.app_factory import create_app
from app.core.config import get_settings

app = create_app()


def run() -> None:
    settings = get_settings()
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.reload,
        # Our structlog config owns logging; uvicorn's would print every line twice.
        log_config=None,
        access_log=False,
    )


if __name__ == "__main__":
    run()
