"""`get_profile`: availability, notice period, preferred roles and locations. Read-only."""

from __future__ import annotations

from pydantic import BaseModel

from app.services.profile import ProfileStore


class ProfileFacts(BaseModel):
    availability: str
    notice_period: str | None
    preferred_roles: list[str]
    locations: list[str]

    def render(self) -> str:
        return "\n".join(
            [
                f"Availability: {self.availability}",
                f"Notice period: {self.notice_period or 'not set'}",
                f"Preferred roles: {', '.join(self.preferred_roles) or 'not set'}",
                f"Locations: {', '.join(self.locations) or 'not set'}",
            ]
        )


class ProfileTool:
    def __init__(self, store: ProfileStore) -> None:
        self._store = store

    async def get_profile(self) -> ProfileFacts:
        profile = await self._store.get()
        return ProfileFacts(
            availability=profile.availability,
            notice_period=profile.notice_period,
            preferred_roles=list(profile.preferred_roles),
            locations=list(profile.locations),
        )
