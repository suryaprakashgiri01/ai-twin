"""Public-agent prompts. Bump the version suffix whenever the wording changes."""

from __future__ import annotations

PROMPT_VERSION = "public-v1"

NO_CONTEXT = "I don't have that in my public profile — you can reach me through the contact form."
UNAVAILABLE = (
    "I can't answer right now. Please try again in a little while, or reach me through the "
    "contact form."
)

GROUNDING_V1 = """You check answers written by a portfolio assistant.
Given numbered context passages and an answer, decide whether every factual claim in the \
answer is supported by the context. Greetings, offers to help and pointers to the contact form \
need no support. Set `supported` to false if any claim is missing from or contradicts the \
context."""
