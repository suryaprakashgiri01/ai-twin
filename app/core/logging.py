"""structlog configuration, bridging the stdlib so third-party loggers match.

A terminal gets coloured, aligned console lines; a pipe (containers, CI) gets one JSON
object per line. ``merge_contextvars`` adds the request/session ids bound by the
middleware, and ``_add_trace_context`` adds the current span's ids when one exists.
"""

from __future__ import annotations

import logging
import sys
from typing import Any, Literal

import structlog
from opentelemetry import trace

_EXTERNAL_LOGGERS = ("uvicorn", "uvicorn.error", "httpx", "httpcore", "pymongo")

# uvicorn's `color_message` carries raw ANSI escapes, which break strict JSON parsers.
_NOISY_RECORD_KEYS = ("color_message",)

# Keys whose values must never reach a log line, whatever the caller passes.
_REDACTED_KEYS = frozenset(
    {"api_key", "x-api-key", "authorization", "password", "secret", "token", "cookie"}
)


def _drop_noisy_keys(
    _logger: object, _method: str, event_dict: structlog.types.EventDict
) -> structlog.types.EventDict:
    for key in _NOISY_RECORD_KEYS:
        event_dict.pop(key, None)
    return event_dict


def _redact_secrets(
    _logger: object, _method: str, event_dict: structlog.types.EventDict
) -> structlog.types.EventDict:
    for key in list(event_dict):
        if key.lower() in _REDACTED_KEYS:
            event_dict[key] = "[REDACTED]"
    return event_dict


def _add_trace_context(
    _logger: object, _method: str, event_dict: structlog.types.EventDict
) -> structlog.types.EventDict:
    """Stamp ``trace_id`` / ``span_id`` when a recording span is current; omit them otherwise."""
    ctx = trace.get_current_span().get_span_context()
    if not ctx.is_valid:
        return event_dict
    event_dict["trace_id"] = format(ctx.trace_id, "032x")
    event_dict["span_id"] = format(ctx.span_id, "016x")
    return event_dict


def resolve_log_format(
    configured: Literal["json", "console", "auto"],
) -> Literal["json", "console"]:
    """Explicit setting wins; otherwise a TTY gets console, a pipe gets JSON."""
    if configured != "auto":
        return configured
    return "console" if sys.stdout.isatty() else "json"


def build_renderer(resolved: Literal["json", "console"]) -> Any:
    if resolved == "json":
        return structlog.processors.JSONRenderer(ensure_ascii=False)
    return structlog.dev.ConsoleRenderer(colors=sys.stdout.isatty())


def setup_logging(
    *,
    level: str = "INFO",
    log_format: Literal["json", "console", "auto"] = "auto",
) -> None:
    numeric_level = getattr(logging, level.upper(), logging.INFO)

    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.ExtraAdder(),
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        _drop_noisy_keys,
        _redact_secrets,
        _add_trace_context,
    ]

    structlog.configure(
        processors=[*shared_processors, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    resolved = resolve_log_format(log_format)
    final_processors: list[structlog.types.Processor] = [
        structlog.stdlib.ProcessorFormatter.remove_processors_meta
    ]
    # The console renderer prints exceptions itself (pretty, coloured); JSON needs them as a field.
    if resolved == "json":
        final_processors.append(structlog.processors.format_exc_info)
    final_processors.append(build_renderer(resolved))
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors, processors=final_processors
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(numeric_level)

    for name in _EXTERNAL_LOGGERS:
        logging.getLogger(name).propagate = True
    for name in ("httpx", "httpcore", "pymongo"):
        logging.getLogger(name).setLevel(logging.WARNING)
    # Our middleware writes the access line, with the request id on it.
    logging.getLogger("uvicorn.access").disabled = True


def get_logger(name: str) -> Any:
    return structlog.get_logger(name)
