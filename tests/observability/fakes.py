"""A Langfuse client whose OTLP exporter is in memory: what would have been sent, inspectable."""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import pytest
from langfuse._client import span as langfuse_span
from langfuse._client import span_processor
from langfuse._client.resource_manager import LangfuseResourceManager
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app.core.config import Settings
from app.core.telemetry import setup_telemetry
from app.observability.tracing import NOOP, LangfuseTracing, install


def attrs_of(span: ReadableSpan) -> dict[str, Any]:
    return dict(span.attributes or {})


def langfuse_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "app_env": "test",
        # The SDK keeps one client per public key; a fresh key per test keeps them apart.
        "langfuse_public_key": f"pk-lf-test-{uuid.uuid4().hex}",
        "langfuse_secret_key": "sk-lf-test",
        "langfuse_base_url": "http://127.0.0.1:9",
        **overrides,
    }
    return Settings(_env_file=None, **values)


@dataclass
class CapturedTraces:
    tracing: LangfuseTracing
    exporter: InMemorySpanExporter
    scores: list[dict[str, Any]] = field(default_factory=list)

    def spans(self) -> list[ReadableSpan]:
        self.tracing.client.flush()
        return list(self.exporter.get_finished_spans())

    def roots(self) -> list[ReadableSpan]:
        return [s for s in self.spans() if s.attributes and "langfuse.trace.name" in s.attributes]

    def generations(self) -> list[ReadableSpan]:
        return [
            s
            for s in self.spans()
            if (s.attributes or {}).get("langfuse.observation.type") == "generation"
        ]

    def text(self) -> str:
        """Every attribute of every span, as one string: what left the machine."""
        return json.dumps(
            [dict(s.attributes or {}) for s in self.spans()], default=str, ensure_ascii=False
        )


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> Iterator[CapturedTraces]:
    yield from capture_with(monkeypatch)


def capture_with(monkeypatch: pytest.MonkeyPatch, **overrides: Any) -> Iterator[CapturedTraces]:
    exporter = InMemorySpanExporter()
    monkeypatch.setattr(span_processor, "OTLPSpanExporter", lambda **_: exporter)
    settings = langfuse_settings(**overrides)
    setup_telemetry(settings)
    tracing = LangfuseTracing(settings)
    result = CapturedTraces(tracing, exporter)

    def score_trace(_self: Any, **kwargs: Any) -> None:
        result.scores.append(kwargs)

    monkeypatch.setattr(langfuse_span.LangfuseObservationWrapper, "score_trace", score_trace)
    install(tracing)
    try:
        yield result
    finally:
        install(NOOP)
        tracing.shutdown()
        LangfuseResourceManager._instances.pop(settings.langfuse_public_key, None)
