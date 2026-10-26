"""Security headers for the web console (/ui): strict CSP, no sniffing, no framing."""

from __future__ import annotations

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

CONSOLE_PREFIX = "/ui"
# Everything the console needs is served from this origin: no CDN, no inline script.
CONSOLE_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
)


def is_console(path: str) -> bool:
    return path == CONSOLE_PREFIX or path.startswith(CONSOLE_PREFIX + "/")


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not is_console(scope["path"]):
            await self.app(scope, receive, send)
            return

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["Content-Security-Policy"] = CONSOLE_CSP
                headers["X-Content-Type-Options"] = "nosniff"
                headers["Referrer-Policy"] = "no-referrer"
                # Revalidate every load (cheap 304 via ETag) so a new build shows at once.
                headers["Cache-Control"] = "no-cache"
            await send(message)

        await self.app(scope, receive, send_wrapper)
