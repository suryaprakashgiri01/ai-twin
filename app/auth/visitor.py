"""Who a public visitor is: the salted hash of the address the portfolio server forwards.

The portfolio's server route holds the service key and sends the visitor's address in
`X-Visitor-Address`. The header is trusted only from a key that carries `public:read` or
`leads:write` itself (an admin key testing the route counts as one visitor), and only its keyed
hash is kept.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass

from fastapi import Request

from app.auth.dependencies import Principal
from app.exceptions import AppError, ErrorCode
from app.services.conversations import hash_visitor

VISITOR_HEADER = "X-Visitor-Address"
TRUSTED_SCOPES: frozenset[str] = frozenset({"public:read", "leads:write"})


@dataclass(frozen=True)
class Visitor:
    hash: str
    # True when the hash comes from the forwarded address, not from the calling key.
    forwarded: bool


def _salt(request: Request) -> str:
    salt: str = request.app.state.settings.visitor_hash_salt.get_secret_value()
    if not salt:
        raise AppError(ErrorCode.DEPENDENCY_UNAVAILABLE, "Visitor hashing is not configured")
    return salt


def _address(raw: str) -> str:
    try:
        return str(ipaddress.ip_address(raw.strip()))
    except ValueError:
        raise AppError(
            ErrorCode.VALIDATION_ERROR, f"{VISITOR_HEADER} must be an IP address"
        ) from None


def resolve_visitor(request: Request, principal: Principal) -> Visitor:
    raw = request.headers.get(VISITOR_HEADER)
    salt = _salt(request)
    if raw and TRUSTED_SCOPES & set(principal.scopes):
        return Visitor(hash=hash_visitor(_address(raw), salt), forwarded=True)
    return Visitor(hash=hash_visitor(f"key:{principal.key_id}", salt), forwarded=False)
