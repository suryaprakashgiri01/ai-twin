from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.exceptions.codes import STATUS_BY_CODE, ErrorCode


class AppError(Exception):
    """An expected failure with a stable code. Rendered as the error envelope."""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        details: Any = None,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details
        # Response headers such as `Retry-After`.
        self.headers = dict(headers or {})

    @property
    def status_code(self) -> int:
        return STATUS_BY_CODE[self.code]
