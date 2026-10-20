"""Read the v2 success envelope in tests: one place that knows its shape."""

from __future__ import annotations

from typing import Any

import httpx


def result(response: httpx.Response) -> Any:
    body = response.json()
    assert body["status"] == "success", body
    return body["result"]


def page(response: httpx.Response) -> dict[str, Any]:
    found: dict[str, Any] = result(response)
    assert set(found) >= {"listOfItems", "page", "pageSize", "total", "totalPages"}, found
    return found


def items(response: httpx.Response) -> list[Any]:
    """The rows of a list page, or a plain list result (batch create)."""
    found = result(response)
    rows: list[Any] = found["listOfItems"] if isinstance(found, dict) else found
    return rows
