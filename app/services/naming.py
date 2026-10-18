"""Readable names for owner conversations: a short hyphenated slug and a one-line description.

Both come from the conversation's first question, with no model call: instant, deterministic and
available without a local LLM. The owner can rename a conversation afterwards.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Collection

NAME_PATTERN = r"^[a-z0-9]+(-[a-z0-9]+)*$"
NAME_MAX = 48
SLUG_MAX = 40
SLUG_WORDS = 4
DESCRIPTION_MAX = 100
FALLBACK_PREFIX = "chat"

_WORD = re.compile(r"[a-z0-9]+")
_SPACE = re.compile(r"\s+")

_STOP_WORDS_TEXT = """
a about above after again all am an and any are as at be been before being below between
both but by can could did do does doing down during each few for from further had has have
having he her here hers him his how i if in into is it its just me more most my no nor not
now of off on once only or other our out over own please same she should so some such tell
than that the their them then there these they this those through to too under until up very
was we were what when where which while who whom why will with would you your yours give
show list find let lets know many much get got also like want need hey hi hello ok okay
thanks thank
"""
STOP_WORDS = frozenset(_STOP_WORDS_TEXT.split())


def slugify_title(text: str, fallback: str) -> str:
    """First few meaningful words of `text`, lower-cased and hyphenated; `fallback` when none."""
    words = [w for w in _WORD.findall(text.lower()) if w not in STOP_WORDS]
    slug = ""
    for word in words[:SLUG_WORDS]:
        candidate = f"{slug}-{word}" if slug else word
        if len(candidate) > SLUG_MAX:
            break
        slug = candidate
    return slug or fallback


def fallback_name(conversation_id: str) -> str:
    return f"{FALLBACK_PREFIX}-{conversation_id[:6]}"


def describe(text: str) -> str:
    """One line of at most DESCRIPTION_MAX characters."""
    line = _SPACE.sub(" ", text).strip()
    if len(line) <= DESCRIPTION_MAX:
        return line
    return line[: DESCRIPTION_MAX - 1].rstrip() + "…"


def unique_name(base: str, taken: Collection[str] | Callable[[str], bool]) -> str:
    """`base`, or `base-2`, `base-3`, … whichever is free."""
    is_taken = taken if callable(taken) else taken.__contains__
    if not is_taken(base):
        return base
    suffix = 2
    while is_taken(f"{base}-{suffix}"):
        suffix += 1
    return f"{base}-{suffix}"
