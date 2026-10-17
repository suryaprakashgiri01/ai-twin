from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.auth.dependencies import Principal, require_scopes
from app.db.documents import Scope

router = APIRouter(tags=["admin"])


class WhoAmI(BaseModel):
    name: str
    scopes: list[Scope]


@router.get("/whoami", summary="The calling key's name and scopes")
async def whoami(principal: Annotated[Principal, Depends(require_scopes())]) -> WhoAmI:
    """Any valid key may call this; it is the smoke test for authentication."""
    return WhoAmI(name=principal.name, scopes=list(principal.scopes))
