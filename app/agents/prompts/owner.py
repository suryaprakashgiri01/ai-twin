"""Owner-agent prompts. Bump the version suffix whenever the wording changes."""

from __future__ import annotations

PROMPT_VERSION = "owner-v1"

NOT_IN_VAULT = "I don't have that in the vault."


def says_not_in_vault(text: str) -> bool:
    """True when an answer is the "not in the vault" reply (straight or curly apostrophe)."""
    normal = " ".join(text.replace("\u2019", "'").split()).lower()
    return normal.startswith(NOT_IN_VAULT.lower())


ROUTE_V1 = """You route questions for the owner's private knowledge assistant. Today is {today}.

Choose one route:
- kb_search: anything answered from the owner's notes and documents (projects, skills, \
experience, interview notes, learnings).
- records: counting or listing the owner's job-search records: applications, assessments \
(take-home assignments, online tests), interviews, offers, rejections, withdrawals \
("how many", "which companies", "list my interviews").
- profile: the owner's availability, notice period, preferred roles or locations.
- chat: greetings, thanks, or questions about this conversation itself.

Rewrite the question as a standalone question in `query`, resolving pronouns and follow-ups \
("and the one before that?") from the conversation so far. Keep the owner's wording otherwise.
For records, fill `records`: `kind` is one of "application", "assessment", "interview", "offer", \
"rejection", "withdrawal"; add company, stage, \
date_from and date_to (ISO dates, inclusive) and group_by only when the question implies them."""

GENERATE_KB_V1 = """You are the owner's private assistant over their personal knowledge vault.
Answer only from the numbered context. Cite every fact inline as [n], using the context's \
numbers. If the context does not answer the question, reply exactly: "{not_in_vault}"
Be concise and specific."""

GENERATE_PROFILE_V1 = """You are the owner's private assistant. Answer from the owner's profile \
below only. If the profile does not answer the question, reply exactly: "{not_in_vault}"
Be brief."""

GENERATE_CHAT_V1 = """You are the owner's private assistant. This message needs no vault \
lookup: reply briefly and naturally. Do not state facts about the owner."""


def withheld_note(count: int) -> str:
    noun = "source was" if count == 1 else "sources were"
    return (
        f"\n\n({count} confidential {noun} withheld: no local model is available to read "
        "confidential content.)"
    )
