"""The error catalog: stable, machine-readable codes, the HTTP status of each and a default message.

Clients branch on `error.code`, never on the message. Codes are renamed only in a breaking change.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ErrorCode(StrEnum):
    INVALID_INPUT = "INVALID_INPUT"
    UNAUTHENTICATED = "UNAUTHENTICATED"
    INVALID_API_KEY = "INVALID_API_KEY"
    FORBIDDEN = "FORBIDDEN"
    NOT_FOUND = "NOT_FOUND"
    METHOD_NOT_ALLOWED = "METHOD_NOT_ALLOWED"
    CONFLICT = "CONFLICT"
    PAYLOAD_TOO_LARGE = "PAYLOAD_TOO_LARGE"
    RATE_LIMITED = "RATE_LIMITED"
    SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"
    # No model may serve the request, or every eligible model failed before answering.
    MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"
    # A model failed after it had started streaming; the partial answer is kept.
    STREAM_INTERRUPTED = "STREAM_INTERRUPTED"
    INTERNAL_ERROR = "INTERNAL_ERROR"


@dataclass(frozen=True)
class ErrorSpec:
    status: int
    message: str


CATALOG: dict[ErrorCode, ErrorSpec] = {
    ErrorCode.INVALID_INPUT: ErrorSpec(422, "The request is not valid"),
    ErrorCode.UNAUTHENTICATED: ErrorSpec(401, "An API key is required"),
    ErrorCode.INVALID_API_KEY: ErrorSpec(401, "The API key is unknown or revoked"),
    ErrorCode.FORBIDDEN: ErrorSpec(403, "Not allowed"),
    ErrorCode.NOT_FOUND: ErrorSpec(404, "Not found"),
    ErrorCode.METHOD_NOT_ALLOWED: ErrorSpec(405, "Method not allowed"),
    ErrorCode.CONFLICT: ErrorSpec(409, "The request conflicts with the current state"),
    ErrorCode.PAYLOAD_TOO_LARGE: ErrorSpec(413, "The request body is too large"),
    ErrorCode.RATE_LIMITED: ErrorSpec(429, "Too many requests; try again later"),
    ErrorCode.SERVICE_UNAVAILABLE: ErrorSpec(503, "A required service is not available"),
    ErrorCode.MODEL_UNAVAILABLE: ErrorSpec(503, "No model can answer right now"),
    ErrorCode.STREAM_INTERRUPTED: ErrorSpec(502, "The answer stopped part way"),
    ErrorCode.INTERNAL_ERROR: ErrorSpec(500, "Internal server error"),
}

STATUS_BY_CODE: dict[ErrorCode, int] = {code: spec.status for code, spec in CATALOG.items()}

# Statuses Starlette raises itself (unknown route, wrong method), mapped onto our codes.
CODE_BY_HTTP_STATUS: dict[int, ErrorCode] = {
    401: ErrorCode.UNAUTHENTICATED,
    403: ErrorCode.FORBIDDEN,
    404: ErrorCode.NOT_FOUND,
    405: ErrorCode.METHOD_NOT_ALLOWED,
    413: ErrorCode.PAYLOAD_TOO_LARGE,
    422: ErrorCode.INVALID_INPUT,
    429: ErrorCode.RATE_LIMITED,
}


def catalog_markdown() -> str:
    """The catalog as a Markdown table, for the OpenAPI description."""
    rows = [
        f"| `{code.value}` | {spec.status} | {spec.message} |" for code, spec in CATALOG.items()
    ]
    return "\n".join(["| Code | Status | Meaning |", "|---|---|---|", *rows])
