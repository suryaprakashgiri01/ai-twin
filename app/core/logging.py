"""structlog configuration, bridging the stdlib so third-party loggers match.

A terminal gets coloured, aligned console lines; a pipe (containers, CI) gets one JSON
object per line. ``merge_contextvars`` adds the request/session ids bound by the
middleware, and ``_add_trace_context`` adds the current span's ids when one exists.
"""

from __future__ import annotations

import logging
import sys
import threading
import time
from collections.abc import Callable
from typing import Any, Literal

import structlog
from opentelemetry import trace

_EXTERNAL_LOGGERS = ("uvicorn", "uvicorn.error", "httpx", "httpcore", "pymongo")

# Presidio attaches its own handlers at import time; ours (the root's) is the only one we keep.
_PRESIDIO_LOGGERS = ("presidio-analyzer", "decision_process")
# The Langfuse SDK adds a console handler to these at import; records reach ours via the root.
_SDK_HANDLER_LOGGERS = ("httpx", "langfuse")
# Exporters (Langfuse, OTLP) and our Langfuse facade: during an outage each failed batch would
# log; together they get one line a minute.
EXPORT_LOGGER_PREFIXES = ("langfuse", "opentelemetry", "app.observability.tracing")

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
    handler.addFilter(PrefixRateLimitFilter(EXPORT_LOGGER_PREFIXES))

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(numeric_level)

    for name in _SDK_HANDLER_LOGGERS:
        drop_own_handlers(name)
    for name in _EXTERNAL_LOGGERS:
        logging.getLogger(name).propagate = True
    for name in ("httpx", "httpcore", "pymongo"):
        logging.getLogger(name).setLevel(logging.WARNING)
    # Our middleware writes the access line, with the request id on it.
    logging.getLogger("uvicorn.access").disabled = True


class OnceFilter(logging.Filter):
    """Lets each distinct message through once per process: a warning repeated per document
    (or per text block) is noise after the first time."""

    def __init__(self) -> None:
        super().__init__()
        self._seen: set[str] = set()
        self._lock = threading.Lock()

    def filter(self, record: logging.LogRecord) -> bool:
        key = f"{record.levelno}:{record.getMessage()}"
        with self._lock:
            if key in self._seen:
                return False
            self._seen.add(key)
        return True


class RateLimitFilter(logging.Filter):
    """At most one record per `interval` seconds through the logger it is attached to.

    For exporters (Langfuse, OTLP) whose every failed batch would otherwise log during an
    outage. The next record let through says how many were dropped.
    """

    def __init__(self, interval: float = 60.0, clock: Callable[[], float] = time.monotonic):
        super().__init__()
        self._interval = interval
        self._clock = clock
        self._last: float | None = None
        self._dropped = 0
        self._lock = threading.Lock()

    def filter(self, record: logging.LogRecord) -> bool:
        with self._lock:
            now = self._clock()
            if self._last is not None and now - self._last < self._interval:
                self._dropped += 1
                return False
            self._last = now
            if self._dropped:
                record.msg = f"{record.getMessage()} ({self._dropped} similar suppressed)"
                record.args = ()
                self._dropped = 0
        return True


class PrefixRateLimitFilter(logging.Filter):
    """A handler filter: records from loggers under `prefixes` share one `RateLimitFilter`
    (warnings and above); everything else passes untouched."""

    def __init__(self, prefixes: tuple[str, ...], interval: float = 60.0) -> None:
        super().__init__()
        self._prefixes = prefixes
        self._limiter = RateLimitFilter(interval)

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno < logging.WARNING or not record.name.startswith(self._prefixes):
            return True
        return self._limiter.filter(record)


def drop_own_handlers(name: str) -> None:
    target = logging.getLogger(name)
    for handler in list(target.handlers):
        if not isinstance(handler, logging.NullHandler):
            target.removeHandler(handler)
    target.propagate = True


def attach_filter(name: str, make: Callable[[], logging.Filter]) -> None:
    """Attach one filter of `make`'s type to logger `name`, once (create_app runs per test)."""
    target = logging.getLogger(name)
    sample = make()
    if not any(type(f) is type(sample) for f in target.filters):
        target.addFilter(sample)


def quiet_presidio() -> None:
    """Call after presidio is imported: drop its own handlers (records reach ours through the
    root, once) and let each warning through once."""
    for name in _PRESIDIO_LOGGERS:
        drop_own_handlers(name)
        attach_filter(name, OnceFilter)
    # Presidio's per-decision trace is debug output; INFO would print one line per entity.
    logging.getLogger("decision_process").setLevel(logging.WARNING)


def get_logger(name: str) -> Any:
    return structlog.get_logger(name)
