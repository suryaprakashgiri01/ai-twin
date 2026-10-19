"""docx, xlsx, pptx, csv and html through markitdown (local conversion, no network, no plugins)."""

from __future__ import annotations

import io
import os
from collections.abc import Callable
from functools import lru_cache
from typing import Any


def without_new_env[T](load: Callable[[], T]) -> T:
    """Run `load`, then drop any environment variable it added.

    magika (a markitdown dependency) calls `load_dotenv()` at import, which would copy every `.env`
    secret into `os.environ`, where child processes inherit them.
    """
    before = set(os.environ)
    try:
        return load()
    finally:
        for key in set(os.environ) - before:
            del os.environ[key]


def _markitdown() -> Any:
    import markitdown

    return markitdown


@lru_cache(maxsize=1)
def _converter() -> Any:
    return without_new_env(_markitdown).MarkItDown(enable_plugins=False)


def to_markdown(data: bytes, kind: str) -> str:
    markitdown = without_new_env(_markitdown)
    result = _converter().convert_stream(
        io.BytesIO(data), stream_info=markitdown.StreamInfo(extension=f".{kind}")
    )
    return str(result.text_content or "")
