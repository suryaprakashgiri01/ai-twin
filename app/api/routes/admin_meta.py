from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from app.api.envelope import ApiModel, Envelope, ok
from app.auth.dependencies import Principal, require_scopes
from app.db.documents import Scope

router = APIRouter(tags=["admin"])


class WhoAmI(ApiModel):
    name: str
    scopes: list[Scope]


@router.get("/whoami", summary="The calling key's name and scopes")
async def whoami(
    principal: Annotated[Principal, Depends(require_scopes())],
) -> Envelope[WhoAmI]:
    """Any valid key may call this; it is the smoke test for authentication."""
    return ok(WhoAmI(name=principal.name, scopes=list(principal.scopes)))
