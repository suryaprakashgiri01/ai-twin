"""The v2 contract, checked on the OpenAPI document: camelCase everywhere and every JSON success
enveloped. A model that forgets the `ApiModel` base fails here, not in a client."""

from __future__ import annotations

from typing import Any

import pytest

from app.api.envelope import Envelope, camel_keys, ok
from app.core.app_factory import create_app
from app.core.config import Settings
from tests.conftest import FakeCheck

PREFIXES = ("/admin/v1", "/v1/")


@pytest.fixture(scope="module")
def spec() -> dict[str, Any]:
    settings = Settings(
        _env_file=None, app_env="test", log_format="json", deployment_profile="vault"
    )
    return create_app(settings, health_checks=[FakeCheck("mongodb")]).openapi()


def _schemas(spec: dict[str, Any]) -> dict[str, Any]:
    schemas: dict[str, Any] = spec["components"]["schemas"]
    return schemas


def _refs(node: Any) -> set[str]:
    if isinstance(node, dict):
        found = {node["$ref"].rsplit("/", 1)[-1]} if "$ref" in node else set()
        for value in node.values():
            found |= _refs(value)
        return found
    if isinstance(node, list):
        return set().union(*(_refs(v) for v in node)) if node else set()
    return set()


def _closure(spec: dict[str, Any], names: set[str]) -> set[str]:
    seen: set[str] = set()
    todo = list(names)
    while todo:
        name = todo.pop()
        if name in seen:
            continue
        seen.add(name)
        todo.extend(_refs(_schemas(spec)[name]) - seen)
    return seen


def _operations(spec: dict[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
    return [
        (path, method, op)
        for path, item in spec["paths"].items()
        if path.startswith(PREFIXES)
        for method, op in item.items()
    ]


def test_api_schemas_have_camel_case_properties(spec: dict[str, Any]) -> None:
    used = set().union(*(_refs(op) for _, _, op in _operations(spec)))
    snake = sorted(
        f"{name}.{prop}"
        for name in _closure(spec, used)
        for prop in _schemas(spec)[name].get("properties", {})
        if "_" in prop
    )
    assert snake == []


def test_query_parameters_are_camel_case(spec: dict[str, Any]) -> None:
    snake = [
        f"{method.upper()} {path} ?{p['name']}"
        for path, method, op in _operations(spec)
        for p in op.get("parameters", [])
        if p["in"] == "query" and "_" in p["name"]
    ]
    assert snake == []


def test_every_json_success_is_enveloped(spec: dict[str, Any]) -> None:
    bare = []
    for path, method, op in _operations(spec):
        for status, response in op["responses"].items():
            schema = response.get("content", {}).get("application/json", {}).get("schema")
            if not status.startswith("2") or not schema:
                continue
            refs = _refs(schema)
            if not any(r.startswith("Envelope") for r in refs):
                bare.append(f"{method.upper()} {path} {status}")
    assert bare == []


def test_ok_dumps_nested_models_by_alias() -> None:
    from app.services.admin_ops import Submitted

    body = ok(Submitted(source_id="s", status="queued", duplicate=False), "Accepted")
    assert isinstance(body, Envelope)
    assert body.model_dump(mode="json", by_alias=True) == {
        "status": "success",
        "message": "Accepted",
        "result": {
            "sourceId": "s",
            "status": "queued",
            "duplicate": False,
            "filename": None,
            "uri": None,
        },
    }


def test_camel_keys_leaves_data_keys_alone() -> None:
    data = {
        "conversation_id": "c",
        "report": {"EMAIL": 1, "role:router": 2},
        "list_x": [{"a_b": 1}],
    }
    assert camel_keys(data) == {
        "conversationId": "c",
        "report": {"EMAIL": 1, "role:router": 2},
        "listX": [{"aB": 1}],
    }
