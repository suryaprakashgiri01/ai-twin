"""Render every failure as one envelope: ``{"error": {code, message, request_id, details}}``."""

from __future__ import annotations

from typing import Any

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from opentelemetry import trace
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.context import get_request_id
from app.exceptions.base import AppError
from app.exceptions.codes import CODE_BY_HTTP_STATUS, ErrorCode

logger = structlog.get_logger(__name__)


def _envelope(
    code: ErrorCode,
    message: str,
    status: int,
    details: Any = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    trace.get_current_span().set_attribute("error.code", code.value)
    body: dict[str, Any] = {
        "code": code.value,
        "message": message,
        "request_id": get_request_id(),
    }
    if details is not None:
        body["details"] = details
    return JSONResponse(status_code=status, content={"error": body}, headers=headers)


async def app_error_handler(_request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, AppError)
    log = logger.error if exc.status_code >= 500 else logger.warning
    log("request failed", error_code=exc.code.value, error_message=exc.message)
    return _envelope(exc.code, exc.message, exc.status_code, exc.details, exc.headers or None)


async def validation_error_handler(_request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    # `input` echoes the caller's payload back; drop it so a pasted secret is not reflected.
    errors = [
        {"loc": list(err.get("loc", ())), "msg": err.get("msg"), "type": err.get("type")}
        for err in exc.errors()
    ]
    logger.warning("request validation failed", error_code="VALIDATION_ERROR", errors=errors)
    return _envelope(ErrorCode.VALIDATION_ERROR, "Request validation failed", 422, errors)


async def http_error_handler(_request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)
    code = CODE_BY_HTTP_STATUS.get(exc.status_code, ErrorCode.INTERNAL_ERROR)
    return _envelope(code, str(exc.detail), exc.status_code)


def render_unhandled(exc: Exception) -> JSONResponse:
    logger.error("unhandled error", error_type=type(exc).__name__, exc_info=exc)
    span = trace.get_current_span()
    span.record_exception(exc)
    span.set_status(trace.Status(trace.StatusCode.ERROR, type(exc).__name__))
    return _envelope(ErrorCode.INTERNAL_ERROR, "Internal server error", 500)


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, app_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_exception_handler(StarletteHTTPException, http_error_handler)
