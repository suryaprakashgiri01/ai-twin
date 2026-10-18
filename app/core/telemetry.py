"""OpenTelemetry: one SDK tracer provider for the process and a server span per request.

The span exists so every log line of a request carries `trace_id` / `span_id` and the response
carries `X-Trace-ID`; the same id seeds the request's Langfuse trace. Spans go nowhere unless
`OTEL_EXPORTER_OTLP_ENDPOINT` is set. An incoming `traceparent` is ignored: a visitor must not
choose the id the owner's traces are filed under.

Langfuse uses its own provider (`app.observability.tracing`), so these spans never reach it.
"""

from __future__ import annotations

import threading

import structlog
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from app.core.config import SERVICE_NAME, Settings

logger = structlog.get_logger(__name__)

TRACE_ID_HEADER = "X-Trace-ID"
_TRACER_NAME = "ai-twin"

_lock = threading.Lock()
_provider: TracerProvider | None = None


def setup_telemetry(settings: Settings) -> TracerProvider:
    """Install the global provider once per process (OTel refuses to replace it)."""
    global _provider
    with _lock:
        if _provider is not None:
            return _provider
        provider = TracerProvider(
            resource=Resource.create(
                {
                    "service.name": SERVICE_NAME,
                    "service.version": settings.service_version,
                    "deployment.environment": settings.app_env,
                }
            )
        )
        if settings.otel_exporter_otlp_endpoint:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

            exporter = OTLPSpanExporter(endpoint=settings.otel_exporter_otlp_endpoint)
            provider.add_span_processor(BatchSpanProcessor(exporter))
            logger.info("otlp span export on")
        trace.set_tracer_provider(provider)
        _provider = provider
        return provider


def tracer() -> trace.Tracer:
    return trace.get_tracer(_TRACER_NAME)


def current_trace_id() -> str | None:
    """The current span's trace id as 32 hex characters, or None outside a span."""
    ctx = trace.get_current_span().get_span_context()
    return format(ctx.trace_id, "032x") if ctx.is_valid else None


def shutdown_telemetry() -> None:
    """Flush the OTLP exporter, if any; the provider itself stays installed."""
    if _provider is not None:
        _provider.force_flush(timeout_millis=3000)
