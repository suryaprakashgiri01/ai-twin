"""Read the v2 success envelope in tests: one place that knows its shape."""

from __future__ import annotations

from typing import Any

import httpx


def result(response: httpx.Response) -> Any:
    body = response.json()
    assert body["status"] == "success", body
    return body["result"]


def items(response: httpx.Response) -> list[Any]:
    found: list[Any] = result(response)
    return found
