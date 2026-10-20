"""Prometheus metrics and `GET /metrics`.

Every label is an enum or a bounded name: route templates (never raw paths), model ids from the
inventory, roles, tiers, outcomes, event kinds. Never an id, a name, an address or any text, so
cardinality stays small and nothing personal reaches a scraper.

In the vault the route is open (it binds to localhost). In the public deployment it needs
`Authorization: Bearer <METRICS_TOKEN>` and answers 404 while no token is configured, so an
unconfigured deployment does not even reveal that it exists.
"""

from __future__ import annotations

import hmac
from collections.abc import Callable, Sequence

from fastapi import APIRouter, Request, Response
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Histogram,
    generate_latest,
)
from prometheus_client.platform_collector import PlatformCollector
from prometheus_client.process_collector import ProcessCollector

from app.core.config import Settings
from app.exceptions import AppError, ErrorCode
from llm_orchestrator import Attempt, Role, Tier
from llm_orchestrator.hooks import AttemptEvent, on_attempt, on_exhausted

REGISTRY = CollectorRegistry(auto_describe=True)
ProcessCollector(registry=REGISTRY)
PlatformCollector(registry=REGISTRY)

_HTTP_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})
_LATENCY_BUCKETS = (0.01, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120)

HTTP_REQUESTS = Counter(
    "aitwin_http_requests",
    "HTTP requests by route template, method and status",
    ["route", "method", "status"],
    registry=REGISTRY,
)
HTTP_LATENCY = Histogram(
    "aitwin_http_request_duration_seconds",
    "HTTP request latency (a streamed response counts until its last byte)",
    ["route", "method"],
    buckets=_LATENCY_BUCKETS,
    registry=REGISTRY,
)
LLM_ATTEMPTS = Counter(
    "aitwin_llm_attempts",
    "Model calls by model, role, tier and outcome",
    ["model", "role", "tier", "outcome"],
    registry=REGISTRY,
)
LLM_LATENCY = Histogram(
    "aitwin_llm_attempt_duration_seconds",
    "Model call latency",
    ["model", "role"],
    buckets=_LATENCY_BUCKETS,
    registry=REGISTRY,
)
LLM_TOKENS = Counter(
    "aitwin_llm_tokens",
    "Tokens by model, role and direction (input or output)",
    ["model", "role", "direction"],
    registry=REGISTRY,
)
LLM_EXHAUSTED = Counter(
    "aitwin_llm_chain_exhausted",
    "Calls where every model in the chain failed",
    ["role", "tier"],
    registry=REGISTRY,
)
GUARD_DECISIONS = Counter(
    "aitwin_guard_decisions",
    "Guard decisions by layer and outcome",
    ["layer", "outcome"],
    registry=REGISTRY,
)
SCOPE_INTENTS = Counter(
    "aitwin_scope_intents", "Public messages by routed intent", ["intent"], registry=REGISTRY
)
ANSWER_CACHE = Counter(
    "aitwin_answer_cache_lookups",
    "Public answer cache lookups by result (hit or miss)",
    ["result"],
    registry=REGISTRY,
)
INGESTION_JOBS = Counter(
    "aitwin_ingestion_jobs", "Ingestion jobs by final status", ["status"], registry=REGISTRY
)
LEADS = Counter(
    "aitwin_leads",
    "Lead submissions by outcome and stored status",
    ["outcome", "status"],
    registry=REGISTRY,
)
EMAILS = Counter(
    "aitwin_emails",
    "Emails to the owner by kind and outcome",
    ["kind", "outcome"],
    registry=REGISTRY,
)
SECURITY_EVENTS = Counter(
    "aitwin_security_events",
    "Security events by kind and severity",
    ["kind", "severity"],
    registry=REGISTRY,
)
ALERT_EMAILS = Counter(
    "aitwin_alert_emails",
    "Alert and digest emails by policy and outcome",
    ["policy", "outcome"],
    registry=REGISTRY,
)


def method_label(method: str) -> str:
    return method if method in _HTTP_METHODS else "OTHER"


def observe_request(route: str, method: str, status: int, seconds: float) -> None:
    method = method_label(method)
    HTTP_REQUESTS.labels(route, method, str(status)).inc()
    HTTP_LATENCY.labels(route, method).observe(seconds)


def _record_attempt(event: AttemptEvent) -> None:
    LLM_ATTEMPTS.labels(event.model_id, event.role, event.tier, event.outcome).inc()
    LLM_LATENCY.labels(event.model_id, event.role).observe(event.latency_ms / 1000)
    if event.input_tokens:
        LLM_TOKENS.labels(event.model_id, event.role, "input").inc(event.input_tokens)
    if event.output_tokens:
        LLM_TOKENS.labels(event.model_id, event.role, "output").inc(event.output_tokens)


def _record_exhausted(role: Role, tier: Tier, _attempts: Sequence[Attempt]) -> None:
    LLM_EXHAUSTED.labels(role.value, tier.label).inc()


_stops: list[Callable[[], None]] = []


def install_llm_metrics() -> None:
    """Count every model call, once per process (create_app may run more than once)."""
    if not _stops:
        _stops.extend([on_attempt(_record_attempt), on_exhausted(_record_exhausted)])


# --- the route --------------------------------------------------------------------------------

router = APIRouter(tags=["observability"])


def _authorise(request: Request, settings: Settings) -> None:
    if settings.deployment_profile == "vault":
        return
    token = settings.metrics_token.get_secret_value()
    if not token:
        raise AppError(ErrorCode.NOT_FOUND, "Not found")
    header = request.headers.get("authorization", "")
    scheme, _, presented = header.partition(" ")
    if scheme.lower() != "bearer" or not hmac.compare_digest(presented.strip(), token):
        raise AppError(ErrorCode.INVALID_API_KEY, "Send the metrics token as a bearer token")


@router.get(
    "/metrics",
    summary="Prometheus metrics",
    response_class=Response,
    responses={200: {"content": {CONTENT_TYPE_LATEST: {}}, "description": "Prometheus text"}},
)
async def metrics(request: Request) -> Response:
    """Prometheus text format. Vault: open (localhost). Public: `Authorization: Bearer
    <METRICS_TOKEN>`; 404 while no token is set, 401 for a wrong one."""
    _authorise(request, request.app.state.settings)
    return Response(generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)
