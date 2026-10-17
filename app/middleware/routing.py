"""Resolve a request to its route template, for low-cardinality labels."""

from __future__ import annotations

from starlette.routing import Match
from starlette.types import Scope

UNMATCHED_ROUTE = "<unmatched>"


def route_template(scope: Scope) -> str:
    """``/tickets/{ticket_id}`` rather than ``/tickets/42``.

    A raw path as a metric label is unbounded cardinality: every id becomes a
    new time series. Unmatched paths (scanners, typos) share one label.
    """
    # FastAPI records the matched route on the scope once routing has run.
    matched = scope.get("route")
    if matched is not None and hasattr(matched, "path"):
        return str(matched.path)
    app = scope.get("app")
    for route in getattr(app, "routes", []):
        match, _ = route.matches(scope)
        if match == Match.FULL:
            return str(getattr(route, "path", UNMATCHED_ROUTE))
    return UNMATCHED_ROUTE
