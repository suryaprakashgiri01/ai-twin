"""What the portfolio widget shows before the first message: greeting, disclosure, privacy."""

from __future__ import annotations

from pydantic import BaseModel

from app.core.config import Settings
from app.services.profile import ProfileStore


class AssistantInfo(BaseModel):
    greeting: str
    # Says plainly that this is an AI, not the owner.
    disclosure: str
    privacy: str
    availability: str


async def assistant_info(settings: Settings, profile: ProfileStore) -> AssistantInfo:
    name = settings.public_owner_name.strip()
    owner = name or "the owner"
    twin = f"{name}'s AI twin" if name else "an AI twin"
    days = settings.public_conversation_retention_days
    return AssistantInfo(
        greeting=f"Hi, I'm {twin}. Ask me about my projects, skills and experience.",
        disclosure=(
            f"You're chatting with an AI twin, not {owner} in person. It answers only from "
            "published profile facts and can make mistakes."
        ),
        privacy=(
            f"Conversations are kept for {days} days, and your address is stored only as a "
            "one-way hash. Messages are processed by third-party AI providers, so please don't "
            "share personal or sensitive details."
        ),
        availability=(await profile.get()).availability,
    )
