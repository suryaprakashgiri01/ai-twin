from __future__ import annotations

from typing import Any

from app.exceptions.codes import STATUS_BY_CODE, ErrorCode


class AppError(Exception):
    """An expected failure with a stable code. Rendered as the error envelope."""

    def __init__(self, code: ErrorCode, message: str, *, details: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details

    @property
    def status_code(self) -> int:
        return STATUS_BY_CODE[self.code]
