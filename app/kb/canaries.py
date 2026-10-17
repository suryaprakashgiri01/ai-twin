"""Canary chunks: one random marker planted in each private or confidential document.

The marker is meaningless text that no honest answer would contain. If the public output guard
ever sees one in a public answer, private data leaked, and that is a critical incident.
"""

from __future__ import annotations

import secrets

from app.kb.ingestion.models import ChunkDraft

CANARY_TIERS = frozenset({"private", "confidential"})
PREFIX = "ref"


def new_marker() -> str:
    return f"{PREFIX}-{secrets.token_hex(4)}-{secrets.token_hex(4)}"


def needs_canary(tier: str) -> bool:
    return tier in CANARY_TIERS


def canary_chunk(ordinal: int, heading: str, marker: str) -> ChunkDraft:
    text = f"{heading}\n\nInternal reference {marker}. Not for publication."
    return ChunkDraft(ordinal=ordinal, text=text, heading=heading, canary=True)
