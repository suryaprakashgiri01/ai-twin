"""Plain text and Markdown: decoded as UTF-8 (a BOM is dropped), line endings normalised."""

from __future__ import annotations


def to_markdown(data: bytes) -> str:
    return data.decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")
