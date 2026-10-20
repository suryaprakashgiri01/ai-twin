"""Render every failure as one envelope:
``{"status": "error", "message", "result": null, "error": {code, requestId, details?}}``."""

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
from app.exceptions.codes import CATALOG, CODE_BY_HTTP_STATUS, ErrorCode

logger = structlog.get_logger(__name__)


def error_body(code: ErrorCode, message: str, details: Any = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code.value, "requestId": get_request_id()}
    if details is not None:
        error["details"] = details
    return {"status": "error", "message": message, "result": None, "error": error}


def _envelope(
    code: ErrorCode,
    message: str,
    status: int,
    details: Any = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    trace.get_current_span().set_attribute("error.code", code.value)
    return JSONResponse(
        status_code=status, content=error_body(code, message, details), headers=headers
    )


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
    code = ErrorCode.INVALID_INPUT
    logger.warning("request validation failed", error_code=code.value, errors=errors)
    return _envelope(code, "Request validation failed", CATALOG[code].status, errors)


async def http_error_handler(_request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)
    code = CODE_BY_HTTP_STATUS.get(exc.status_code, ErrorCode.INTERNAL_ERROR)
    headers = dict(exc.headers) if exc.headers else None
    return _envelope(code, str(exc.detail), exc.status_code, headers=headers)


def render_unhandled(exc: Exception) -> JSONResponse:
    logger.error("unhandled error", error_type=type(exc).__name__, exc_info=exc)
    span = trace.get_current_span()
    span.record_exception(exc)
    span.set_status(trace.Status(trace.StatusCode.ERROR, type(exc).__name__))
    code = ErrorCode.INTERNAL_ERROR
    return _envelope(code, CATALOG[code].message, CATALOG[code].status)


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, app_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_exception_handler(StarletteHTTPException, http_error_handler)
