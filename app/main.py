"""Process entrypoint: ``python -m app.main``."""

from __future__ import annotations

import argparse
from collections.abc import Sequence

import uvicorn

from app.core.app_factory import create_app
from app.core.config import get_settings

app = create_app()


def run(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run the API")
    # A flag rather than `RELOAD=true make run`, which Windows cmd.exe cannot parse.
    parser.add_argument("--reload", action="store_true", help="restart on code changes")
    args = parser.parse_args(argv)
    settings = get_settings()
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.reload or args.reload,
        # Our structlog config owns logging; uvicorn's would print every line twice.
        log_config=None,
        access_log=False,
    )


if __name__ == "__main__":
    run()
