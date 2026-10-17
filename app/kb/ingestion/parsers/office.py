"""docx, xlsx, pptx, csv and html through markitdown (local conversion, no network, no plugins)."""

from __future__ import annotations

import io
from functools import lru_cache
from typing import Any


@lru_cache(maxsize=1)
def _converter() -> Any:
    from markitdown import MarkItDown

    return MarkItDown(enable_plugins=False)


def to_markdown(data: bytes, kind: str) -> str:
    from markitdown import StreamInfo

    result = _converter().convert_stream(
        io.BytesIO(data), stream_info=StreamInfo(extension=f".{kind}")
    )
    return str(result.text_content or "")
