"""Correlation id + access log, as raw ASGI middleware.

Raw ASGI rather than ``BaseHTTPMiddleware`` so the contextvars it binds are
visible to the endpoint, exception handlers, and anything they spawn, and so
the access line is written even when the response body streams.
"""

from __future__ import annotations

import re
import time
import uuid

import structlog
from opentelemetry import trace
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.context import set_request_id, set_session_id, set_user_id
from app.exceptions.handlers import render_unhandled
from app.middleware.routing import route_template

REQUEST_ID_HEADER = "X-Request-ID"
# Groups several requests (a conversation, a test run) into one session in logs and
# traces. Defaults to the request id.
SESSION_ID_HEADER = "X-Session-ID"

# Accept a caller's id only if it is short and printable; anything else is a
# log-injection vector, so it is replaced rather than trusted.
_VALID_REQUEST_ID = re.compile(r"^[A-Za-z0-9._:\-]{1,128}$")

# Probe and scrape traffic would drown the access log at INFO.
_QUIET_PATHS = frozenset({"/healthz", "/readyz"})

logger = structlog.get_logger("app.access")


def resolve_request_id(raw: str | None) -> str:
    if raw and _VALID_REQUEST_ID.match(raw):
        return raw
    return uuid.uuid4().hex


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        request_id = resolve_request_id(headers.get(REQUEST_ID_HEADER.lower()))
        raw_session = headers.get(SESSION_ID_HEADER.lower())
        session_id = (
            raw_session if raw_session and _VALID_REQUEST_ID.match(raw_session) else request_id
        )

        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id, session_id=session_id)
        set_request_id(request_id)
        set_session_id(session_id)
        set_user_id(None)
        # Tag the current server span (once tracing is wired) so a request id finds its trace.
        span = trace.get_current_span()
        span.set_attribute("request.id", request_id)
        span.set_attribute("session.id", session_id)

        status_code = 500
        response_started = False
        started = time.perf_counter()

        async def send_wrapper(message: Message) -> None:
            nonlocal status_code, response_started
            if message["type"] == "http.response.start":
                status_code = message["status"]
                response_started = True
                response_headers = MutableHeaders(scope=message)
                response_headers[REQUEST_ID_HEADER] = request_id
                response_headers[SESSION_ID_HEADER] = session_id
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception as exc:
            # Rendered here rather than by Starlette's ServerErrorMiddleware, which
            # sits outside this middleware: there the envelope would lose the
            # request id header and the log line its context.
            if response_started:
                raise
            response = render_unhandled(exc)
            await response(scope, receive, send_wrapper)
        finally:
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            path = scope["path"]
            fields = {
                "method": scope["method"],
                "path": path,
                "route": route_template(scope),
                "status": status_code,
                "duration_ms": duration_ms,
            }
            if path in _QUIET_PATHS and status_code < 400:
                logger.debug("request completed", **fields)
            elif status_code >= 500:
                logger.error("request completed", **fields)
            elif status_code >= 400:
                logger.warning("request completed", **fields)
            else:
                logger.info("request completed", **fields)
            structlog.contextvars.clear_contextvars()
