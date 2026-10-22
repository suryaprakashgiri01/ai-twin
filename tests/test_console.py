"""owner-console T1: the console is served safely, and any key can learn its role."""

from __future__ import annotations

import pytest

from app.auth.keys import issue_key
from app.middleware.security_headers import CONSOLE_CSP
from tests.envelope import result
from tests.public.test_routes_limits import client_for, public_app


@pytest.mark.parametrize("profile", ["vault", "public"])
async def test_console_is_served_with_security_headers_on_both_profiles(profile: str) -> None:
    app, _, _ = public_app(profile)  # type: ignore[arg-type]
    async for client in client_for(app):
        page = await client.get("/ui/")
        assert page.status_code == 200 and "ai-twin console" in page.text
        assert page.headers["content-security-policy"] == CONSOLE_CSP
        assert page.headers["x-content-type-options"] == "nosniff"
        # Static files carry no request id: the request middleware stays out of the way.
        assert "x-request-id" not in page.headers
        root = await client.get("/", follow_redirects=False)
        assert root.status_code == 307 and root.headers["location"] == "/ui/"
        assert (await client.get("/ui/nope.js")).status_code == 404
        # An API route still answers JSON with its own headers.
        health = await client.get("/healthz")
        assert health.status_code == 200 and "content-security-policy" not in health.headers


@pytest.mark.parametrize(
    ("scopes", "role"),
    [(["admin"], "owner"), (["public:read"], "public"), (["leads:write"], "leads")],
)
async def test_me_gives_every_key_its_role(scopes: list[str], role: str) -> None:
    app, keys, _ = public_app("public")
    key = (await issue_key(keys, "tester", scopes))[1]  # type: ignore[arg-type]
    async for client in client_for(app):
        me = await client.get("/v1/me", headers={"X-API-Key": key})
        assert me.status_code == 200
        assert result(me) == {"name": "tester", "scopes": scopes, "role": role}
        assert (await client.get("/v1/me")).status_code == 401
        assert (await client.get("/v1/me", headers={"X-API-Key": "atw_nope"})).status_code == 401
