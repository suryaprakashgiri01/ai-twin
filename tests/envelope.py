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


def success(result: Any) -> httpx.Response:
    """A fake server's v2 success response."""
    return httpx.Response(200, json={"status": "success", "message": "OK", "result": result})


def page_of(rows: list[dict[str, Any]], request: httpx.Request) -> httpx.Response:
    """A fake server's first page of `rows`, sized by the request's `pageSize`."""
    size = int(request.url.params.get("pageSize", 20))
    return success(
        {
            "listOfItems": rows[:size],
            "page": 1,
            "pageSize": size,
            "total": len(rows),
            "totalPages": -(-len(rows) // size),
            "sortColumn": "updatedAt",
            "sortOrder": "desc",
        }
    )
