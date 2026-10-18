"""Resolve a request to its route template, for low-cardinality labels."""

from __future__ import annotations

import re

from starlette.routing import Match
from starlette.types import Scope

UNMATCHED_ROUTE = "<unmatched>"
_PARAM = re.compile(r"\{(\w+)(?::\w+)?\}")


def _with_prefix(template: str, scope: Scope) -> str:
    """The template with its routers' prefixes. Included routers resolve lazily, so the matched
    route knows only its own path (`/leads/{lead_id}`); the request path holds the prefixes
    (`/admin/v1`), which are static, in front of the rendered template."""
    params = scope.get("path_params") or {}
    rendered = _PARAM.sub(lambda m: str(params.get(m.group(1), m.group(0))), template)
    path = str(scope.get("path", ""))
    if rendered and path.endswith(rendered):
        return path[: len(path) - len(rendered)] + template
    return template


def route_template(scope: Scope) -> str:
    """``/tickets/{ticket_id}`` rather than ``/tickets/42``.

    A raw path as a metric label is unbounded cardinality: every id becomes a
    new time series. Unmatched paths (scanners, typos) share one label.
    """
    # FastAPI records the matched route on the scope once routing has run.
    matched = scope.get("route")
    if matched is not None and hasattr(matched, "path"):
        return _with_prefix(str(matched.path), scope)
    app = scope.get("app")
    for route in getattr(app, "routes", []):
        match, _ = route.matches(scope)
        if match == Match.FULL:
            return str(getattr(route, "path", UNMATCHED_ROUTE))
    return UNMATCHED_ROUTE
