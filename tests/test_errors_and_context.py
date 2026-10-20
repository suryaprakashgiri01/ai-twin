from __future__ import annotations

import httpx
from fastapi import FastAPI
from pydantic import BaseModel

from app.exceptions import AppError, ErrorCode
from tests.conftest import make_app


class Body(BaseModel):
    name: str


def _app_with_routes() -> FastAPI:
    app = make_app()

    @app.get("/boom")
    async def boom() -> None:
        raise AppError(ErrorCode.FORBIDDEN, "nope", details={"why": "test"})

    @app.get("/crash")
    async def crash() -> None:
        raise RuntimeError("secret detail")

    @app.post("/echo")
    async def echo(body: Body) -> Body:
        return body

    return app


async def _client() -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=_app_with_routes(), raise_app_exceptions=False)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


async def test_unknown_path_renders_not_found_envelope() -> None:
    async with await _client() as c:
        response = await c.get("/nope", headers={"X-Request-ID": "req-123"})
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"
    assert response.json()["error"]["requestId"] == "req-123"


async def test_app_error_keeps_code_status_and_details() -> None:
    async with await _client() as c:
        response = await c.get("/boom")
    assert response.status_code == 403
    assert response.json() == {
        "status": "error",
        "message": "nope",
        "result": None,
        "error": {
            "code": "FORBIDDEN",
            "requestId": response.headers["X-Request-ID"],
            "details": {"why": "test"},
        },
    }


async def test_wrong_method_is_method_not_allowed() -> None:
    async with await _client() as c:
        response = await c.delete("/boom")
    assert response.status_code == 405
    assert response.json()["error"]["code"] == "METHOD_NOT_ALLOWED"
    assert response.headers["allow"] == "GET"


def test_every_code_is_in_the_catalog() -> None:
    from app.exceptions.codes import CATALOG, STATUS_BY_CODE

    assert set(CATALOG) == set(ErrorCode)
    assert all(400 <= status < 600 for status in STATUS_BY_CODE.values())


async def test_validation_error_does_not_echo_input() -> None:
    async with await _client() as c:
        response = await c.post("/echo", json={"name": 1, "password": "hunter2"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_INPUT"
    assert "hunter2" not in response.text


async def test_unhandled_error_is_generic_500() -> None:
    async with await _client() as c:
        response = await c.get("/crash")
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "INTERNAL_ERROR"
    assert "secret detail" not in response.text


async def test_valid_ids_echoed_invalid_replaced() -> None:
    async with await _client() as c:
        good = await c.get("/healthz", headers={"X-Request-ID": "abc", "X-Session-ID": "s1"})
        bad = await c.get("/healthz", headers={"X-Request-ID": "bad id\n"})
    assert good.headers["X-Request-ID"] == "abc"
    assert good.headers["X-Session-ID"] == "s1"
    assert bad.headers["X-Request-ID"] != "bad id\n"
    assert len(bad.headers["X-Request-ID"]) == 32
