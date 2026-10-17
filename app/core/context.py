"""Request-scoped values, readable anywhere in the call stack.

structlog's contextvars carry them onto log lines; these accessors serve the
code that needs the values themselves (error envelopes, response bodies).
"""

from __future__ import annotations

from contextvars import ContextVar

_request_id: ContextVar[str | None] = ContextVar("request_id", default=None)
_user_id: ContextVar[str | None] = ContextVar("user_id", default=None)
_session_id: ContextVar[str | None] = ContextVar("session_id", default=None)


def get_request_id() -> str | None:
    return _request_id.get()


def set_request_id(value: str | None) -> None:
    _request_id.set(value)


def get_user_id() -> str | None:
    return _user_id.get()


def set_user_id(value: str | None) -> None:
    _user_id.set(value)


def get_session_id() -> str | None:
    return _session_id.get()


def set_session_id(value: str | None) -> None:
    _session_id.set(value)
