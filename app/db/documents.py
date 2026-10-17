"""Beanie documents: one class per MongoDB collection, indexes declared on each."""

from __future__ import annotations

from beanie import Document

# Every collection Beanie manages; startup creates their indexes.
DOCUMENT_MODELS: list[type[Document]] = []
