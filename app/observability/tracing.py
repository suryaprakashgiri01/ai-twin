"""Langfuse tracing behind a small facade: one trace per agent run, LLM calls as generations.

`tracing().run(name, channel, ...)` opens a run: a Langfuse root span whose trace id is the
request's OpenTelemetry trace id (so `X-Trace-ID`, log lines and the Langfuse trace match),
plus a LangChain `CallbackHandler` installed through a configure hook, so every model call and
LangGraph node inside the run is recorded under it without passing callbacks around. Tags,
metadata and the output are set when the run ends; scores go out as they are known.

Masking happens in the Langfuse client (`app.observability.masking`), by the run's channel.
Langfuse keeps its own TracerProvider, so the app's request spans never reach it.

Without `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY` and `LANGFUSE_BASE_URL` every call is a
no-op. A Langfuse failure never fails a request: each call is guarded, and failures (the SDK's
exporter included, see `app.core.logging`) log at most once a minute.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Mapping, Sequence
from contextvars import ContextVar
from types import TracebackType
from typing import Any, Literal, Protocol

import structlog
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.tracers.context import register_configure_hook
from langfuse import Langfuse
from langfuse.langchain import CallbackHandler
from langfuse.types import TraceContext
from opentelemetry.sdk.trace import TracerProvider

from app.core.config import Settings
from app.core.telemetry import current_trace_id
from app.observability.masking import Channel, Masker, MaskPolicy, reset_channel, set_channel

logger = structlog.get_logger(__name__)

ScoreValue = float | str | bool
ScoreType = Literal["NUMERIC", "CATEGORICAL", "BOOLEAN"]
WARN_INTERVAL_SECONDS = 60.0

# The run's LangChain handler; LangChain adds it to every callback manager configured while set.
_handler: ContextVar[BaseCallbackHandler | None] = ContextVar("langfuse_handler", default=None)
register_configure_hook(_handler, inheritable=True)


class TraceRun:
    """A run that records nothing: what every call gets while tracing is off."""

    trace_id: str | None = None

    def __enter__(self) -> TraceRun:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None

    def tag(self, *tags: str) -> None:
        """Add tags (short enums: route, intent, outcome)."""

    def meta(self, **values: Any) -> None:
        """Add metadata: ids, enums, counts, rule names; never text."""

    def output(self, value: Any) -> None:
        """The run's output, masked by its channel's policy."""

    def restrict(self, channel: Channel) -> None:
        """Trace the rest of the run, and its input and output, under `channel`'s policy (a
        public turn that turns out to carry a lead goes under the lead policy)."""

    def score(
        self,
        name: str,
        value: ScoreValue,
        *,
        data_type: ScoreType | None = None,
        comment: str | None = None,
    ) -> None:
        """A score on the trace (grounding, guard outcome, intake outcome, lead triage)."""


NOOP_RUN = TraceRun()
_run: ContextVar[TraceRun | None] = ContextVar("langfuse_run", default=None)


class Tracing(Protocol):
    @property
    def enabled(self) -> bool: ...

    def run(
        self,
        name: str,
        channel: Channel,
        *,
        input: Any = None,
        session_id: str | None = None,
        tags: Sequence[str] = (),
        metadata: Mapping[str, Any] | None = None,
    ) -> TraceRun: ...

    def shutdown(self) -> None: ...

    async def trace_url(self, trace_id: str) -> str | None:
        """The Langfuse page for a trace, or None when tracing is off or Langfuse is unreachable."""
        ...


class NoopTracing:
    enabled = False

    def run(
        self,
        name: str,
        channel: Channel,
        *,
        input: Any = None,
        session_id: str | None = None,
        tags: Sequence[str] = (),
        metadata: Mapping[str, Any] | None = None,
    ) -> TraceRun:
        return NOOP_RUN

    def shutdown(self) -> None:
        return None

    async def trace_url(self, trace_id: str) -> str | None:
        return None


class _Warner:
    """One warning per interval, with how many failures it stands for."""

    def __init__(self, interval: float = WARN_INTERVAL_SECONDS) -> None:
        self._interval = interval
        self._last: float | None = None
        self._dropped = 0
        self._lock = threading.Lock()

    def __call__(self, action: str, exc: BaseException) -> None:
        with self._lock:
            now = time.monotonic()
            if self._last is not None and now - self._last < self._interval:
                self._dropped += 1
                return
            self._last, dropped, self._dropped = now, self._dropped, 0
        logger.warning(
            "langfuse call failed", action=action, error_type=type(exc).__name__, dropped=dropped
        )


class LangfuseRun(TraceRun):
    def __init__(
        self,
        owner: LangfuseTracing,
        name: str,
        channel: Channel,
        *,
        input: Any,
        session_id: str | None,
        tags: Sequence[str],
        metadata: Mapping[str, Any] | None,
    ) -> None:
        self._owner = owner
        self._name = name
        self._channel = channel
        self._input = input
        self._session_id = session_id
        self._tags = [f"channel:{channel}", *tags]
        self._metadata: dict[str, Any] = {"channel": channel, **owner.base_metadata}
        self._metadata.update(metadata or {})
        self._output: Any = None
        self._root: Any = None
        # Set when this run started inside another one: it is a span of that trace, not a trace.
        self._parent: LangfuseRun | None = None
        self._tokens: list[tuple[ContextVar[Any], Any]] = []
        self._channel_tokens: list[Any] = []

    def __enter__(self) -> TraceRun:
        parent = _run.get()
        if isinstance(parent, LangfuseRun) and parent._root is not None:
            self._parent = parent
        self._channel_tokens.append(set_channel(self._channel))
        self._tokens.append((_run, _run.set(self)))
        try:
            self._root = self._start()
            handler = CallbackHandler(
                public_key=self._owner.public_key,
                trace_context=TraceContext(
                    trace_id=str(self.trace_id), parent_span_id=self._root.id
                ),
            )
        except Exception as exc:
            self._owner.warn("start", exc)
            self._root = None
            return self
        self._tokens.append((_handler, _handler.set(handler)))
        return self

    def _start(self) -> Any:
        context: TraceContext
        if self._parent is not None:
            self.trace_id = self._parent.trace_id
            context = TraceContext(
                trace_id=str(self.trace_id), parent_span_id=self._parent._root.id
            )
        else:
            self.trace_id = current_trace_id() or self._owner.client.create_trace_id()
            context = TraceContext(trace_id=self.trace_id)
        # The input is set when the run ends, under the channel it ended with.
        return self._owner.client.start_span(
            name=self._name, trace_context=context, metadata=dict(self._metadata)
        )

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        try:
            if self._root is not None:
                self._finish(exc_type)
        except Exception as error:
            self._owner.warn("end", error)
        finally:
            for var, token in reversed(self._tokens):
                try:
                    var.reset(token)
                except ValueError:
                    var.set(None)
            for token in reversed(self._channel_tokens):
                reset_channel(token)

    def _finish(self, exc_type: type[BaseException] | None) -> None:
        root = self._root
        if self._parent is not None:
            # A nested run names nothing at trace level; its tags join the trace's.
            self._parent.tag(*self._tags)
        else:
            self._update_trace(root)
        if exc_type is not None and not issubclass(exc_type, GeneratorExit):
            root.update(level="ERROR", status_message=exc_type.__name__)
        root.update(input=self._input, output=self._output, metadata=dict(self._metadata))
        root.end()

    def _update_trace(self, root: Any) -> None:
        root.update_trace(
            name=self._name,
            session_id=self._session_id,
            tags=sorted(set(self._tags)),
            metadata=dict(self._metadata),
            input=self._input,
            output=self._output,
        )

    def tag(self, *tags: str) -> None:
        self._tags.extend(t for t in tags if t)

    def restrict(self, channel: Channel) -> None:
        if channel == self._channel:
            return
        self._channel = channel
        self._metadata["channel"] = channel
        self._tags.append(f"channel:{channel}")
        self._channel_tokens.append(set_channel(channel))

    def meta(self, **values: Any) -> None:
        self._metadata.update(values)

    def output(self, value: Any) -> None:
        self._output = value

    def score(
        self,
        name: str,
        value: ScoreValue,
        *,
        data_type: ScoreType | None = None,
        comment: str | None = None,
    ) -> None:
        if self._root is None:
            return
        if isinstance(value, bool):
            value, data_type = float(value), "BOOLEAN"
        try:
            self._root.score_trace(name=name, value=value, data_type=data_type, comment=comment)
        except Exception as exc:
            self._owner.warn("score", exc)


class LangfuseTracing:
    enabled = True

    def __init__(self, settings: Settings, *, tracer_provider: TracerProvider | None = None):
        self.public_key = settings.langfuse_public_key
        self.base_metadata = {"env": settings.app_env, "version": settings.service_version}
        self.warn = _Warner()
        self.client = Langfuse(
            public_key=settings.langfuse_public_key,
            secret_key=settings.langfuse_secret_key.get_secret_value(),
            base_url=settings.langfuse_base_url,
            environment=settings.app_env,
            release=settings.service_version,
            sample_rate=settings.langfuse_sample_rate,
            timeout=5,
            mask=Masker(
                MaskPolicy(
                    owner_content=settings.langfuse_owner_content,
                    public_content=settings.langfuse_public_content,
                )
            ),
            # Its own provider: the app's request spans must not flood Langfuse.
            tracer_provider=tracer_provider or TracerProvider(),
        )

    def run(
        self,
        name: str,
        channel: Channel,
        *,
        input: Any = None,
        session_id: str | None = None,
        tags: Sequence[str] = (),
        metadata: Mapping[str, Any] | None = None,
    ) -> TraceRun:
        return LangfuseRun(
            self,
            name,
            channel,
            input=input,
            session_id=session_id,
            tags=tags,
            metadata=metadata,
        )

    def flush(self) -> None:
        try:
            self.client.flush()
        except Exception as exc:
            self.warn("flush", exc)

    def shutdown(self) -> None:
        try:
            self.client.shutdown()
        except Exception as exc:
            self.warn("shutdown", exc)

    async def trace_url(self, trace_id: str) -> str | None:
        # The SDK looks the project id up once (a network call), then builds the link.
        try:
            url = await asyncio.to_thread(self.client.get_trace_url, trace_id=trace_id)
        except Exception as exc:
            self.warn("trace_url", exc)
            return None
        return str(url) if url else None


NOOP = NoopTracing()
_tracing: Tracing = NOOP


def langfuse_configured(settings: Settings) -> bool:
    return bool(
        settings.langfuse_public_key
        and settings.langfuse_secret_key.get_secret_value()
        and settings.langfuse_base_url
    )


def build_tracing(settings: Settings) -> Tracing:
    if not langfuse_configured(settings):
        return NOOP
    tracing_ = LangfuseTracing(settings)
    logger.info(
        "langfuse tracing on",
        owner_content=settings.langfuse_owner_content,
        public_content=settings.langfuse_public_content,
        sample_rate=settings.langfuse_sample_rate,
    )
    return tracing_


def tracing() -> Tracing:
    """The process's tracing (no-op until the app installs one)."""
    return _tracing


def install(value: Tracing) -> None:
    global _tracing
    _tracing = value


def current_run() -> TraceRun:
    """The run in progress in this context, or a no-op one: scores and tags from deep in a
    graph land on the right trace without passing it around."""
    return _run.get() or NOOP_RUN
