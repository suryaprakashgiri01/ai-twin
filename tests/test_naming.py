from __future__ import annotations

from app.services.naming import (
    DESCRIPTION_MAX,
    describe,
    fallback_name,
    slugify_title,
    unique_name,
)


def test_slug_keeps_first_meaningful_words() -> None:
    title = slugify_title("What did I discuss in my System Design interview prep?", "x")
    assert title == "discuss-system-design-interview"


def test_slug_respects_length_and_falls_back() -> None:
    long = slugify_title("supercalifragilisticexpialidocious antidisestablishmentarianism ok", "x")
    assert len(long) <= 40 and long.startswith("supercalifragilisticexpialidocious")
    assert slugify_title("how are you?", "chat-abc123") == "chat-abc123"
    assert fallback_name("abcdef0123456789" * 2) == "chat-abcdef"


def test_description_is_one_trimmed_line() -> None:
    assert describe("  two\n lines  ") == "two lines"
    long = describe("word " * 60)
    assert len(long) == DESCRIPTION_MAX and long.endswith("…")


def test_unique_name_adds_the_next_free_suffix() -> None:
    assert unique_name("interview-prep", set()) == "interview-prep"
    assert unique_name("interview-prep", {"interview-prep"}) == "interview-prep-2"
    taken = {"interview-prep", "interview-prep-2"}
    assert unique_name("interview-prep", taken) == "interview-prep-3"
    assert unique_name("a", lambda n: n == "a") == "a-2"
