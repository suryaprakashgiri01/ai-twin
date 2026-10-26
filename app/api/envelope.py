"""The success envelope and the camelCase base shared by every API model.

Every JSON success under `/admin/v1` and `/v1` is ``{"status": "success", "message", "result"}``.
API models serialise camelCase and accept either camelCase or snake_case input; `model_dump()`
without `by_alias` stays snake_case, so storage, audit details and prompts are unaffected.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

# Only identifiers like `latency_ms`: map keys such as `EMAIL` or `role:router` are data.
_SNAKE = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)+$")


class ApiModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class Envelope[T](ApiModel):
    status: Literal["success"] = "success"
    message: str = "OK"
    result: T


class Deleted(ApiModel):
    id: str


def ok[T](result: T, message: str = "OK") -> Envelope[T]:
    return Envelope[T](result=result, message=message)


def camel_keys(value: Any) -> Any:
    """`value` with snake_case dict keys turned camelCase, recursively (for SSE event data)."""
    if isinstance(value, dict):
        return {
            (to_camel(k) if isinstance(k, str) and _SNAKE.match(k) else k): camel_keys(v)
            for k, v in value.items()
        }
    if isinstance(value, list | tuple):
        return [camel_keys(v) for v in value]
    return value
